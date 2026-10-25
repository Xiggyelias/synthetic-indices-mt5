"""
Research database
=================
SQLite (WAL mode) with versioned SQL migrations in qb2/migrations. SQLite keeps
the research environment self-contained and reproducible; the schema uses
portable types so it can be moved to PostgreSQL by replaying the same files with
AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _json(obj: Any) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if hasattr(o, "__dataclass_fields__"):
            return o.__dict__
        return str(o)
    return json.dumps(_scrub(obj), default=default)


def _scrub(obj: Any) -> Any:
    """Replace NaN/inf with None so stored JSON stays standard."""
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return _scrub(dict(obj.__dict__))
    return obj


class Database:
    def __init__(self, path: str = "data/quantum_brain.db"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    # ── Migrations ───────────────────────────────────────────────────────────

    def migrate(self) -> List[str]:
        """Apply pending migrations in filename order. Returns the ones applied now."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            done = {r[0] for r in self._conn.execute("SELECT name FROM schema_migrations")}
            applied = []
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in done:
                    continue
                self._conn.executescript(f.read_text(encoding="utf-8"))
                self._conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (f.name, time.time()))
                self._conn.commit()
                applied.append(f.name)
            return applied

    # ── Instruments ──────────────────────────────────────────────────────────

    def upsert_instrument(self, inst: Dict[str, Any], notes: str = "") -> None:
        rf = inst.get("rise_fall_5m")
        self.execute(
            """INSERT INTO instruments (symbol, display_name, market, submarket, pip_size, is_open,
                                        rise_fall_5m, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET display_name=excluded.display_name, market=excluded.market,
                 submarket=excluded.submarket, pip_size=excluded.pip_size, is_open=excluded.is_open,
                 rise_fall_5m=excluded.rise_fall_5m, notes=excluded.notes, updated_at=excluded.updated_at""",
            (inst["symbol"], inst.get("display_name", ""), inst.get("market"), inst.get("submarket"),
             inst.get("pip_size"), int(bool(inst.get("is_open", True))),
             None if rf is None else int(bool(rf)), notes, time.time()),
        )

    def instruments(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM instruments ORDER BY symbol")]

    # ── Ticks & lineage ──────────────────────────────────────────────────────

    def start_run(self, kind: str, source: str, symbol: Optional[str] = None,
                  params: Optional[Dict[str, Any]] = None) -> int:
        cur = self.execute(
            "INSERT INTO ingestion_runs (kind, symbol, params, source, started_at) VALUES (?, ?, ?, ?, ?)",
            (kind, symbol, _json(params or {}), source, time.time()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, rows: int, notes: str = "") -> None:
        self.execute(
            "UPDATE ingestion_runs SET finished_at=?, status=?, rows_inserted=?, notes=? WHERE id=?",
            (time.time(), status, int(rows), notes, run_id),
        )

    def insert_ticks(self, symbol: str, epochs: Iterable[int], quotes: Iterable[float],
                     run_id: Optional[int] = None) -> int:
        """Insert raw ticks; an existing (symbol, epoch) is kept. Returns rows inserted."""
        rows = [(symbol, int(e), float(q), run_id) for e, q in zip(epochs, quotes)]
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO ticks (symbol, epoch, quote, run_id) VALUES (?, ?, ?, ?)", rows
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def load_ticks(self, symbol: str, start: Optional[float] = None, end: Optional[float] = None) -> pd.DataFrame:
        sql, params = "SELECT epoch, quote FROM ticks WHERE symbol=?", [symbol]
        if start is not None:
            sql += " AND epoch >= ?"
            params.append(int(start))
        if end is not None:
            sql += " AND epoch <= ?"
            params.append(int(end))
        with self._lock:
            df = pd.read_sql_query(sql + " ORDER BY epoch", self._conn, params=params)
        return df.astype({"epoch": "int64", "quote": "float64"})

    def tick_summary(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT symbol, COUNT(*) AS ticks, MIN(epoch) AS first_epoch, MAX(epoch) AS last_epoch "
            "FROM ticks GROUP BY symbol ORDER BY symbol"
        )]

    def ingestion_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (limit,))]

    # ── Quotes ───────────────────────────────────────────────────────────────

    def record_quote(self, q: Dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO contract_quotes (symbol, contract_type, stake, payout, duration_seconds, currency,
                                            quote_time, spot, spot_time, proposal_id, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (q["symbol"], q["contract_type"], q["stake"], q["payout"], q["duration_seconds"], q["currency"],
             q["quote_time"], q.get("spot"), q.get("spot_time"), q.get("proposal_id"), q.get("source", "deriv_proposal")),
        )

    def load_quotes(self, symbol: str, duration_seconds: Optional[int] = None) -> pd.DataFrame:
        sql, params = "SELECT * FROM contract_quotes WHERE symbol=?", [symbol]
        if duration_seconds is not None:
            sql += " AND duration_seconds=?"
            params.append(int(duration_seconds))
        with self._lock:
            return pd.read_sql_query(sql + " ORDER BY quote_time", self._conn, params=params)

    # ── Audit, decisions, trades ─────────────────────────────────────────────

    def audit(self, event_type: str, payload: Dict[str, Any], severity: str = "info") -> None:
        self.execute(
            "INSERT INTO audit_events (created_at, severity, event_type, payload) VALUES (?, ?, ?, ?)",
            (time.time(), severity, event_type, _json(payload)),
        )

    def audit_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))]

    def record_decision(self, decision: Dict[str, Any], mode: str) -> int:
        cur = self.execute(
            "INSERT INTO decisions (symbol, decided_at, mode, action, contract_type, model_version, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (decision["symbol"], decision["decided_at"], mode, decision["action"], decision.get("contract_type"),
             decision.get("model_version"), _json(decision)),
        )
        return int(cur.lastrowid)

    def decisions(self, limit: int = 200, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM decisions", []
        if symbol:
            sql += " WHERE symbol=?"
            params.append(symbol)
        rows = self.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def insert_trade(self, t: Dict[str, Any]) -> int:
        if t["mode"] not in ("paper", "demo"):
            raise ValueError(f"Refusing to record a trade in mode {t['mode']!r}")
        cur = self.execute(
            """INSERT INTO trades (mode, symbol, contract_type, idempotency_key, stake, payout, start_time,
                                   expiry_time, status, contract_id, model_version, decision_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (t["mode"], t["symbol"], t["contract_type"], t["idempotency_key"], t["stake"], t["payout"],
             t["start_time"], t["expiry_time"], t.get("contract_id"), t.get("model_version"), t.get("decision_id")),
        )
        return int(cur.lastrowid)

    def settle_trade(self, trade_id: int, status: str, profit: float, entry_spot: Optional[float],
                     exit_spot: Optional[float]) -> None:
        self.execute(
            "UPDATE trades SET status=?, profit=?, entry_spot=?, exit_spot=?, settled_at=? WHERE id=? AND status='open'",
            (status, profit, entry_spot, exit_spot, time.time(), trade_id),
        )

    def trades(self, mode: Optional[str] = None, status: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        sql, params, where = "SELECT * FROM trades", [], []
        if mode:
            where.append("mode=?")
            params.append(mode)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [dict(r) for r in self.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]

    # ── Key-value state (risk governor, strategy halts) ──────────────────────

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv_state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, _json(value), time.time()),
        )

    # ── Models ───────────────────────────────────────────────────────────────

    def insert_model(self, m: Dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO models (version, symbol, model_type, status, feature_version, dataset_hash,
                                   artifact_path, artifact_sha256, metrics, created_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m["version"], m["symbol"], m["model_type"], m["status"], m["feature_version"], m["dataset_hash"],
             m["artifact_path"], m["artifact_sha256"], _json(m["metrics"]), time.time(), m.get("notes", "")),
        )

    def update_model_status(self, version: str, status: str, approved_by: Optional[str] = None,
                            notes: Optional[str] = None) -> None:
        if approved_by is not None:
            self.execute("UPDATE models SET status=?, approved_by=?, approved_at=?, notes=COALESCE(?, notes) WHERE version=?",
                         (status, approved_by, time.time(), notes, version))
        else:
            self.execute("UPDATE models SET status=?, notes=COALESCE(?, notes) WHERE version=?", (status, notes, version))

    def models(self, symbol: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params, where = "SELECT * FROM models", [], []
        if symbol:
            where.append("symbol=?")
            params.append(symbol)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.query(sql + " ORDER BY created_at DESC", params)
        return [dict(r, metrics=json.loads(r["metrics"])) for r in rows]

    def model(self, version: str) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT * FROM models WHERE version=?", (version,))
        return dict(rows[0], metrics=json.loads(rows[0]["metrics"])) if rows else None

    # ── Holdout, drift, reports, quality ─────────────────────────────────────

    def record_holdout_use(self, dataset_hash: str, model_version: Optional[str], purpose: str) -> None:
        self.execute("INSERT INTO holdout_uses (dataset_hash, model_version, used_at, purpose) VALUES (?, ?, ?, ?)",
                     (dataset_hash, model_version, time.time(), purpose))

    def holdout_uses(self, dataset_hash: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM holdout_uses WHERE dataset_hash=? ORDER BY id", (dataset_hash,))]

    def record_drift(self, symbol: str, model_version: Optional[str], status: str, report: Dict[str, Any]) -> None:
        self.execute("INSERT INTO drift_events (created_at, symbol, model_version, status, report) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), symbol, model_version, status, _json(report)))

    def drift_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM drift_events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r, report=json.loads(r["report"])) for r in rows]

    def save_report(self, kind: str, payload: Dict[str, Any]) -> int:
        cur = self.execute("INSERT INTO research_reports (kind, created_at, payload) VALUES (?, ?, ?)",
                           (kind, time.time(), _json(payload)))
        return int(cur.lastrowid)

    def latest_report(self, kind: str) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT * FROM research_reports WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
        return dict(rows[0], payload=json.loads(rows[0]["payload"])) if rows else None

    def reports(self, kind: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM research_reports WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def save_quality_report(self, symbol: str, passed: bool, report: Dict[str, Any]) -> None:
        self.execute("INSERT INTO data_quality_reports (symbol, created_at, passed, report) VALUES (?, ?, ?, ?)",
                     (symbol, time.time(), int(passed), _json(report)))

    def latest_quality(self) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT q.* FROM data_quality_reports q JOIN (SELECT symbol, MAX(id) AS id FROM data_quality_reports "
            "GROUP BY symbol) m ON q.id = m.id ORDER BY q.symbol"
        )
        return [dict(r, report=json.loads(r["report"])) for r in rows]
