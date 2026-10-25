"""
Self-Calibration of Win Probabilities
=====================================
Every model is wrong somewhere. This module compares the win probability the brain
claimed for each closed trade with what actually happened, per strategy archetype,
and scales future claims by the observed ratio.

Beta-binomial shrinkage keeps it stable: the record starts with `prior_strength`
pseudo-trades that performed exactly as claimed, so a handful of results moves the
factor only a little, while a long record dominates.

    factor = (wins + k·p̄) / (Σ claimed + k·p̄)      p̄ = mean claimed probability
    p_adjusted = p_claimed · clip(factor, min_factor, max_factor)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

import numpy as np


class ProbabilityCalibrator:
    def __init__(
        self,
        path: Optional[str] = None,
        prior_strength: float = 20.0,
        min_factor: float = 0.3,
        max_factor: float = 1.2,
    ):
        self.path = Path(path) if path else None
        self.prior_strength = prior_strength
        self.min_factor = min_factor
        self.max_factor = max_factor
        self.logger = logging.getLogger("synthetic-calibration")
        self.stats: Dict[str, Dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            self.stats = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("Could not load calibration record %s: %s", self.path, exc)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a crash mid-write cannot corrupt the record
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.stats, f, indent=2)
        os.replace(tmp, self.path)

    def record(self, archetype: str, claimed_p: float, won: bool) -> None:
        s = self.stats.setdefault(archetype, {"n": 0, "wins": 0, "claimed_sum": 0.0})
        s["n"] += 1
        s["wins"] += int(bool(won))
        s["claimed_sum"] += float(claimed_p)
        self._save()

    def factor(self, archetype: str) -> float:
        s = self.stats.get(archetype)
        if not s or s["n"] <= 0 or s["claimed_sum"] <= 0:
            return 1.0
        mean_claimed = s["claimed_sum"] / s["n"]
        prior = self.prior_strength * mean_claimed
        raw = (s["wins"] + prior) / (s["claimed_sum"] + prior)
        return float(np.clip(raw, self.min_factor, self.max_factor))

    def adjust(self, archetype: str, claimed_p: float) -> float:
        return float(np.clip(claimed_p * self.factor(archetype), 0.0, 1.0))

    def summary(self) -> Dict[str, Dict[str, float]]:
        return {
            arch: {
                "trades": s["n"],
                "realised_win_rate": round(s["wins"] / s["n"], 3) if s["n"] else 0.0,
                "mean_claimed": round(s["claimed_sum"] / s["n"], 3) if s["n"] else 0.0,
                "factor": round(self.factor(arch), 3),
            }
            for arch, s in self.stats.items()
        }
