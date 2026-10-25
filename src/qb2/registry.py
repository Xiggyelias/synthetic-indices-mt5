r"""
Model registry and promotion
============================
Lifecycle:  candidate --(acceptance gates)--> validated --(human approval)--> champion
                      \--> rejected                                 champion --> retired

  - Training only ever creates candidates. Nothing in the codebase promotes
    automatically; promote() needs a validated model and a named approver.
  - Artifacts are versioned files with a SHA-256 recorded in the database; a
    tampered or corrupted artifact refuses to load.
  - rollback() restores the most recent retired champion (also needs an approver).
  - Every transition is written to the audit log.
"""

from __future__ import annotations

import hashlib
import pickle
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qb2.models import TrainedModel
from qb2.storage import Database
from qb2.validation import AcceptanceGates, acceptance


class RegistryError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ModelRegistry:
    def __init__(self, db: Database, model_dir: str, gates: AcceptanceGates = AcceptanceGates()):
        self.db = db
        self.dir = Path(model_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.gates = gates

    def register_candidate(self, trained: TrainedModel, metrics: Dict[str, Any], notes: str = "") -> Tuple[str, str]:
        """Save a candidate, run the acceptance gates, return (version, status)."""
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        version = f"{trained.symbol}-{trained.model_type}-{ts}-{trained.dataset_hash[:8]}-{uuid.uuid4().hex[:6]}"
        path = self.dir / f"{version}.pkl"
        with open(path, "wb") as f:
            pickle.dump(trained, f, protocol=pickle.HIGHEST_PROTOCOL)
        passed, failures = acceptance(metrics, self.gates)
        status = "validated" if passed else "rejected"
        metrics = dict(metrics, acceptance={"passed": passed, "failures": failures})
        self.db.insert_model({
            "version": version, "symbol": trained.symbol, "model_type": trained.model_type,
            "status": status, "feature_version": trained.feature_version,
            "dataset_hash": trained.dataset_hash, "artifact_path": str(path),
            "artifact_sha256": _sha256(path), "metrics": metrics, "notes": notes,
        })
        self.db.audit("model_registered", {"version": version, "status": status, "failures": failures})
        return version, status

    def load(self, version: str) -> TrainedModel:
        meta = self.db.model(version)
        if meta is None:
            raise RegistryError(f"Unknown model {version}")
        path = Path(meta["artifact_path"])
        if not path.exists():
            raise RegistryError(f"Artifact missing: {path}")
        if _sha256(path) != meta["artifact_sha256"]:
            self.db.audit("model_artifact_tampered", {"version": version}, "critical")
            raise RegistryError(f"Artifact hash mismatch for {version}; refusing to load")
        # Artifacts are written by this registry and verified by hash before unpickling
        with open(path, "rb") as f:
            return pickle.load(f)

    def champion(self, symbol: str) -> Optional[Dict[str, Any]]:
        rows = self.db.models(symbol=symbol, status="champion")
        return rows[0] if rows else None

    def promote(self, version: str, approver: str, note: str = "") -> None:
        if not approver or not approver.strip():
            raise RegistryError("Promotion requires a named human approver")
        meta = self.db.model(version)
        if meta is None:
            raise RegistryError(f"Unknown model {version}")
        if meta["status"] != "validated":
            raise RegistryError(f"{version} is {meta['status']}; only validated models can be promoted")
        self.load(version)  # verifies the artifact before it can serve decisions
        current = self.champion(meta["symbol"])
        if current is not None:
            self.db.update_model_status(current["version"], "retired", notes=f"replaced by {version}")
        self.db.update_model_status(version, "champion", approved_by=approver.strip(), notes=note or None)
        self.db.audit("model_promoted", {"version": version, "approver": approver, "replaced": current and current["version"],
                                         "note": note}, "warning")

    def rollback(self, symbol: str, approver: str, note: str = "") -> str:
        if not approver or not approver.strip():
            raise RegistryError("Rollback requires a named human approver")
        retired = self.db.models(symbol=symbol, status="retired")
        if not retired:
            raise RegistryError(f"No retired champion to roll back to for {symbol}")
        target = max(retired, key=lambda m: m["approved_at"] or 0)
        self.load(target["version"])
        current = self.champion(symbol)
        if current is not None:
            self.db.update_model_status(current["version"], "retired", notes=f"rolled back by {approver}")
        self.db.update_model_status(target["version"], "champion", approved_by=approver.strip(), notes=note or "rollback")
        self.db.audit("model_rollback", {"symbol": symbol, "to": target["version"],
                                         "from": current and current["version"], "approver": approver}, "warning")
        return target["version"]

    def demote(self, symbol: str, reason: str) -> Optional[str]:
        """Retire the champion (e.g. on a drift HALT). Safe to call automatically: it only reduces trading."""
        current = self.champion(symbol)
        if current is None:
            return None
        self.db.update_model_status(current["version"], "retired", notes=f"demoted: {reason}")
        self.db.audit("model_demoted", {"version": current["version"], "reason": reason}, "critical")
        return current["version"]

    def list(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.db.models(symbol=symbol)
