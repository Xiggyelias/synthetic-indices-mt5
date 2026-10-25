"""
Settings with safe defaults
===========================
Defaults: TRADING_MODE=paper, DEMO_ONLY=true, ENABLE_TRADING=false, USD,
five-minute Rise/Fall, one concurrent position.

Real-money trading is not a configurable option. TRADING_MODE accepts only
"paper" and "demo", DEMO_ONLY must stay true, and anything else raises
ConfigError at load time instead of falling back to some other mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ALLOWED_TRADING_MODES = ("paper", "demo")
DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

LEGACY_WS_URL = "wss://ws.derivws.com/websockets/v3"
# New API, unauthenticated market data (active_symbols, ticks, ticks_history, contracts_for,
# proposal). Verified live 2026-10-24; the legacy host answered HTTP 520 from this machine.
PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"


class ConfigError(ValueError):
    pass


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Not a boolean: {value!r}")


@dataclass(frozen=True)
class RiskLimits:
    """Deterministic limits enforced by the RiskGovernor, independent of any model."""
    max_stake_fraction: float = 0.01          # stake <= 1% of current equity
    min_stake: float = 0.35                   # Deriv's usual minimum Rise/Fall stake in USD
    max_stake: float = 25.0
    max_daily_loss_fraction: float = 0.03     # of start-of-day equity, realized
    max_drawdown_fraction: float = 0.10       # from peak equity
    max_trades_per_day: int = 20
    max_concurrent_positions: int = 1
    consecutive_loss_limit: int = 4
    cooldown_minutes: float = 60.0
    max_tick_age_seconds: float = 10.0
    max_quote_age_seconds: float = 3.0
    reconciliation_tolerance: float = 0.01    # USD
    max_disconnects_per_hour: int = 5
    max_abnormal_responses: int = 3

    def validate(self) -> None:
        if not 0 < self.max_stake_fraction <= 0.05:
            raise ConfigError("max_stake_fraction must be in (0, 0.05]")
        if self.min_stake <= 0 or self.max_stake < self.min_stake:
            raise ConfigError("Need 0 < min_stake <= max_stake")
        if not 0 < self.max_daily_loss_fraction < 1 or not 0 < self.max_drawdown_fraction < 1:
            raise ConfigError("Loss limits must be fractions in (0, 1)")
        if self.max_concurrent_positions < 1 or self.max_trades_per_day < 1:
            raise ConfigError("Position and trade-count limits must be >= 1")


@dataclass(frozen=True)
class Settings:
    trading_mode: str = "paper"
    demo_only: bool = True
    enable_trading: bool = False
    currency: str = "USD"
    contract_family: str = "RISE_FALL"
    duration: int = 5
    duration_unit: str = "m"

    deriv_app_id: str = ""
    deriv_api_token: str = field(default="", repr=False)
    deriv_account_id: str = ""                # new API: account whose OTP URL the demo workflow requests
    deriv_ws_url: str = PUBLIC_WS_URL
    deriv_api_version: str = "new"            # "new" or "legacy" (renamed fields, see schema.py)

    db_path: str = "data/quantum_brain.db"
    model_dir: str = "data/qb2_models"
    symbols: tuple = ()                       # empty = discover from the API

    decision_interval_seconds: int = 60
    execution_latency_seconds: float = 1.0
    min_edge_fraction: float = 0.02           # EV lower bound must exceed 2% of stake
    uncertainty_z: float = 1.0                # p is lowered by z·σ before pricing
    risk: RiskLimits = field(default_factory=RiskLimits)

    @property
    def contract_seconds(self) -> int:
        return int(self.duration) * DURATION_UNIT_SECONDS[self.duration_unit]

    @property
    def ws_url_with_app_id(self) -> str:
        if self.deriv_api_version == "new" or "app_id=" in self.deriv_ws_url:
            return self.deriv_ws_url
        if not self.deriv_app_id:
            raise ConfigError("DERIV_APP_ID is required for the legacy WebSocket endpoint")
        sep = "&" if "?" in self.deriv_ws_url else "?"
        return f"{self.deriv_ws_url}{sep}app_id={self.deriv_app_id}"

    def validate(self) -> "Settings":
        if self.trading_mode not in ALLOWED_TRADING_MODES:
            raise ConfigError(
                f"TRADING_MODE={self.trading_mode!r} is not supported. Allowed: {ALLOWED_TRADING_MODES}. "
                "Real-money execution is not implemented in Quantum Brain v2."
            )
        if not self.demo_only:
            raise ConfigError("DEMO_ONLY must be true. Real-money execution is not implemented.")
        if self.contract_family != "RISE_FALL":
            raise ConfigError("Only the RISE_FALL contract family is implemented")
        if self.duration_unit not in DURATION_UNIT_SECONDS or self.duration <= 0:
            raise ConfigError("Contract duration must be a positive time duration (s, m or h), not ticks")
        if self.deriv_api_version not in ("legacy", "new"):
            raise ConfigError("DERIV_API_VERSION must be 'legacy' or 'new'")
        if self.uncertainty_z < 0 or self.min_edge_fraction < 0:
            raise ConfigError("uncertainty_z and min_edge_fraction must be non-negative")
        self.risk.validate()
        return self

    @property
    def execution_enabled(self) -> bool:
        """True only for the isolated demo workflow with every flag set deliberately."""
        return self.enable_trading and self.trading_mode == "demo" and self.demo_only

    def public_dict(self) -> Dict[str, Any]:
        """Settings for logs and the dashboard; the API token is never included."""
        return {
            "trading_mode": self.trading_mode,
            "demo_only": self.demo_only,
            "enable_trading": self.enable_trading,
            "execution_enabled": self.execution_enabled,
            "currency": self.currency,
            "contract": f"{self.contract_family} {self.duration}{self.duration_unit}",
            "deriv_api_version": self.deriv_api_version,
            "token_configured": bool(self.deriv_api_token),
            "decision_interval_seconds": self.decision_interval_seconds,
            "min_edge_fraction": self.min_edge_fraction,
            "uncertainty_z": self.uncertainty_z,
            "risk": self.risk.__dict__,
        }


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[str] = None,
) -> Settings:
    """
    Settings from an optional JSON file, overridden by environment variables.
    Secrets (DERIV_API_TOKEN) are read from the environment only.
    """
    env = os.environ if env is None else env
    file_cfg: Dict[str, Any] = {}
    path = config_path or env.get("QB2_CONFIG")
    if path and Path(path).exists():
        file_cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        if "deriv_api_token" in file_cfg:
            raise ConfigError("Do not put DERIV_API_TOKEN in a config file; use the environment")

    risk_cfg = dict(file_cfg.pop("risk", {}) or {})
    for key in RiskLimits.__dataclass_fields__:
        env_key = f"QB2_RISK_{key.upper()}"
        if env.get(env_key):
            risk_cfg[key] = env[env_key]
    risk = RiskLimits(**{
        k: type(getattr(RiskLimits, k))(v) for k, v in risk_cfg.items() if k in RiskLimits.__dataclass_fields__
    })

    def pick(key: str, env_key: str, default: Any) -> Any:
        if env.get(env_key) not in (None, ""):
            return env[env_key]
        return file_cfg.get(key, default)

    symbols = pick("symbols", "QB2_SYMBOLS", ())
    if isinstance(symbols, str):
        symbols = tuple(s.strip() for s in symbols.split(",") if s.strip())

    settings = Settings(
        trading_mode=str(pick("trading_mode", "TRADING_MODE", "paper")).strip().lower(),
        demo_only=_bool(pick("demo_only", "DEMO_ONLY", True), True),
        enable_trading=_bool(pick("enable_trading", "ENABLE_TRADING", False), False),
        currency=str(pick("currency", "QB2_CURRENCY", "USD")).upper(),
        contract_family=str(pick("contract_family", "QB2_CONTRACT_FAMILY", "RISE_FALL")).upper(),
        duration=int(pick("duration", "QB2_DURATION", 5)),
        duration_unit=str(pick("duration_unit", "QB2_DURATION_UNIT", "m")),
        deriv_app_id=str(pick("deriv_app_id", "DERIV_APP_ID", "")),
        deriv_api_token=env.get("DERIV_API_TOKEN", ""),
        deriv_account_id=str(pick("deriv_account_id", "DERIV_ACCOUNT_ID", "")),
        deriv_ws_url=str(pick("deriv_ws_url", "DERIV_WS_URL",
                              LEGACY_WS_URL if str(pick("deriv_api_version", "DERIV_API_VERSION", "new")).lower() == "legacy"
                              else PUBLIC_WS_URL)),
        deriv_api_version=str(pick("deriv_api_version", "DERIV_API_VERSION", "new")).lower(),
        db_path=str(pick("db_path", "QB2_DB_PATH", "data/quantum_brain.db")),
        model_dir=str(pick("model_dir", "QB2_MODEL_DIR", "data/qb2_models")),
        symbols=tuple(symbols),
        decision_interval_seconds=int(pick("decision_interval_seconds", "QB2_DECISION_INTERVAL", 60)),
        execution_latency_seconds=float(pick("execution_latency_seconds", "QB2_LATENCY_SECONDS", 1.0)),
        min_edge_fraction=float(pick("min_edge_fraction", "QB2_MIN_EDGE_FRACTION", 0.02)),
        uncertainty_z=float(pick("uncertainty_z", "QB2_UNCERTAINTY_Z", 1.0)),
        risk=risk,
    )
    return settings.validate()


def with_overrides(settings: Settings, **kwargs: Any) -> Settings:
    """Copy with changes, re-validated so overrides cannot bypass the safety checks."""
    return replace(settings, **kwargs).validate()
