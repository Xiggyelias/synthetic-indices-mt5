"""
Deriv API schema normalization
==============================
Deriv runs two WebSocket APIs. Checked against developers.deriv.com
(comparison pages, 2026-10):

  active_symbols   legacy symbol / display_name / pip / symbol_type
                   new    underlying_symbol / underlying_symbol_name / pip_size /
                          underlying_symbol_type
  proposal, buy    request field `symbol` renamed to `underlying_symbol` in the new API;
                   ask_price, payout and commission may arrive as strings or numbers
  proposal_open_contract
                   numeric fields may be strings; sell_spot(_time) deprecated in favour
                   of exit_spot(_time)
  ticks_history    unchanged shape; default count dropped from 5000 to 1000

Every parser here accepts both forms, so the rest of the system sees one schema.

Rise/Fall: CALL = Rise (exit spot strictly above entry spot), PUT = Fall
(strictly below). A tie loses both. CALLE/PUTE ("allow equals") are not used.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

RISE, FALL = "CALL", "PUT"
RISE_FALL_TYPES = (RISE, FALL)


def num(value: Any) -> Optional[float]:
    """Parse a field that the new API may send as `string | number`."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def first(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


@dataclass
class Instrument:
    symbol: str
    display_name: str
    market: str
    submarket: str
    pip_size: Optional[float]
    is_open: bool
    symbol_type: str = ""
    rise_fall_5m: Optional[bool] = None       # None until contracts_for is checked

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_active_symbol(d: Dict[str, Any]) -> Instrument:
    pip = num(first(d, "pip_size", "pip"))
    is_open = first(d, "exchange_is_open", default=1)
    return Instrument(
        symbol=str(first(d, "underlying_symbol", "symbol", default="")),
        display_name=str(first(d, "underlying_symbol_name", "display_name", default="")),
        market=str(first(d, "market", default="")),
        submarket=str(first(d, "submarket", default="")),
        pip_size=pip,
        is_open=bool(int(is_open)) if str(is_open).isdigit() else bool(is_open),
        symbol_type=str(first(d, "underlying_symbol_type", "symbol_type", default="")),
    )


def is_volatility_index(inst: Instrument) -> bool:
    """Continuous Volatility Indices (R_10..R_100 and the 1s variants), not Jump/Step/Boom/Crash."""
    name = inst.display_name.lower()
    if inst.market and inst.market != "synthetic_index":
        return False
    if inst.submarket and inst.submarket not in ("random_index", "continuous_indices"):
        return False
    return name.startswith("volatility") and "index" in name


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdt])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_seconds(text: Any) -> Optional[int]:
    """'15s' -> 15, '5m' -> 300, '1d' -> 86400. Tick durations ('5t') are not time: None."""
    m = _DURATION_RE.match(str(text or ""))
    if not m or m.group(2) == "t":
        return None
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def rise_fall_available(contracts_for: Dict[str, Any], duration_seconds: int,
                        api_version: str = "legacy") -> Tuple[bool, List[str]]:
    """
    Does contracts_for offer CALL and PUT whose time duration range covers
    `duration_seconds`? Returns (available, notes).

    Legacy lists barrier-free Rise/Fall entries separately (barriers = 0). The new API's
    simplified metadata lists CALL/PUT only with a default barrier (barriers = 1; checked
    live 2026-10), so there the barrier count is ignored and availability must be
    confirmed with a barrier-free proposal (see confirms_rise_fall).
    """
    available = (contracts_for or {}).get("available", []) or []
    found: Dict[str, bool] = {}
    notes: List[str] = []
    for c in available:
        ctype = str(c.get("contract_type", "")).upper()
        if ctype not in RISE_FALL_TYPES:
            continue
        if str(c.get("contract_category", "callput")) != "callput":
            continue
        if api_version == "legacy" and int(num(c.get("barriers", 0)) or 0) != 0:
            continue  # Higher/Lower variants carry a barrier
        lo = parse_duration_seconds(c.get("min_contract_duration"))
        hi = parse_duration_seconds(c.get("max_contract_duration"))
        if lo is None or hi is None:
            notes.append(f"{ctype}: tick-only or unparsed duration {c.get('min_contract_duration')}..{c.get('max_contract_duration')}")
            continue
        if lo <= duration_seconds <= hi:
            found[ctype] = True
        else:
            notes.append(f"{ctype}: {duration_seconds}s outside {lo}s..{hi}s")
    ok = all(found.get(t) for t in RISE_FALL_TYPES)
    if ok:
        return True, []
    return False, notes or ["No non-barrier CALL/PUT offered"]


def confirms_rise_fall(quote: "ContractQuote") -> bool:
    """
    A priced proposal is a genuine Rise/Fall when its contract text states the
    settlement rule this system models, e.g. "Win payout if Volatility 100 Index is
    strictly higher than entry spot at 5 minutes after contract start time."
    """
    text = quote.longcode.lower()
    word = "strictly higher" if quote.contract_type == RISE else "strictly lower"
    return word in text and "entry spot" in text and "after contract start time" in text


def normalize_ticks_history(resp: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray]:
    """(epochs int64, quotes float64) from a ticks_history response with style=ticks."""
    hist = resp.get("history") or {}
    times = hist.get("times") or []
    prices = hist.get("prices") or []
    if len(times) != len(prices):
        raise ValueError(f"ticks_history length mismatch: {len(times)} times vs {len(prices)} prices")
    epochs = np.asarray([int(num(t)) for t in times], dtype=np.int64)
    quotes = np.asarray([num(p) for p in prices], dtype=float)
    return epochs, quotes


def normalize_tick(msg: Dict[str, Any]) -> Optional[Tuple[str, int, float]]:
    t = msg.get("tick") or {}
    sym = first(t, "underlying_symbol", "symbol")
    epoch, quote = num(t.get("epoch")), num(t.get("quote"))
    if not sym or epoch is None or quote is None:
        return None
    return str(sym), int(epoch), float(quote)


@dataclass
class ContractQuote:
    """A priced Rise/Fall proposal. `stake` is the ask price; `payout` includes the stake."""
    symbol: str
    contract_type: str
    stake: float
    payout: float
    duration_seconds: int
    currency: str
    quote_time: float                       # local receive time (epoch seconds)
    proposal_id: str = ""
    spot: Optional[float] = None
    spot_time: Optional[int] = None
    date_start: Optional[int] = None
    longcode: str = ""
    source: str = "deriv_proposal"          # or "historical_proposal", "assumed"

    @property
    def breakeven_probability(self) -> float:
        return self.stake / self.payout if self.payout > 0 else 1.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_proposal(
    resp: Dict[str, Any],
    symbol: str,
    contract_type: str,
    duration_seconds: int,
    currency: str,
    received_at: Optional[float] = None,
) -> ContractQuote:
    p = resp.get("proposal") or {}
    if not p.get("id"):
        raise ValueError("proposal response without id")
    ask, payout = num(p.get("ask_price")), num(p.get("payout"))
    if ask is None or payout is None:
        raise ValueError("proposal response without ask_price/payout")
    spot_time = num(p.get("spot_time"))
    date_start = num(p.get("date_start"))
    return ContractQuote(
        symbol=symbol,
        contract_type=contract_type,
        stake=ask,
        payout=payout,
        duration_seconds=duration_seconds,
        currency=currency,
        quote_time=received_at if received_at is not None else time.time(),
        proposal_id=str(p["id"]),
        spot=num(p.get("spot")),
        spot_time=int(spot_time) if spot_time is not None else None,
        date_start=int(date_start) if date_start is not None else None,
        longcode=str(p.get("longcode", "")),
    )


@dataclass
class AccountInfo:
    loginid: str
    is_virtual: bool
    currency: str
    balance: Optional[float]
    scopes: List[str] = field(default_factory=list)


def normalize_authorize(resp: Dict[str, Any]) -> AccountInfo:
    a = resp.get("authorize") or {}
    return AccountInfo(
        loginid=str(a.get("loginid", "")),
        is_virtual=bool(int(num(a.get("is_virtual")) or 0)),
        currency=str(a.get("currency", "")),
        balance=num(a.get("balance")),
        scopes=[str(s) for s in (a.get("scopes") or [])],
    )


@dataclass
class ContractStatus:
    contract_id: str
    contract_type: str
    status: str                      # open, won, lost, sold, ...
    is_sold: bool
    buy_price: Optional[float]
    payout: Optional[float]
    profit: Optional[float]
    entry_spot: Optional[float]
    entry_time: Optional[int]
    exit_spot: Optional[float]
    exit_time: Optional[int]
    date_start: Optional[int]
    date_expiry: Optional[int]


def normalize_open_contract(resp: Dict[str, Any]) -> ContractStatus:
    c = resp.get("proposal_open_contract") or {}

    def as_int(*keys: str) -> Optional[int]:
        v = num(first(c, *keys))
        return int(v) if v is not None else None

    return ContractStatus(
        contract_id=str(c.get("contract_id", "")),
        contract_type=str(c.get("contract_type", "")),
        status=str(c.get("status", "open")),
        is_sold=bool(int(num(c.get("is_sold")) or 0)),
        buy_price=num(c.get("buy_price")),
        payout=num(c.get("payout")),
        profit=num(c.get("profit")),
        entry_spot=num(first(c, "entry_spot", "entry_tick")),
        entry_time=as_int("entry_spot_time", "entry_tick_time"),
        exit_spot=num(first(c, "exit_spot", "exit_tick", "sell_spot")),
        exit_time=as_int("exit_spot_time", "exit_tick_time", "sell_spot_time"),
        date_start=as_int("date_start"),
        date_expiry=as_int("date_expiry"),
    )


def proposal_request(
    symbol: str,
    contract_type: str,
    stake: float,
    duration: int,
    duration_unit: str,
    currency: str,
    api_version: str = "legacy",
) -> Dict[str, Any]:
    """A Rise/Fall proposal request with the symbol field the chosen API expects."""
    if contract_type not in RISE_FALL_TYPES:
        raise ValueError(f"Not a Rise/Fall contract type: {contract_type}")
    sym_key = "underlying_symbol" if api_version == "new" else "symbol"
    return {
        "proposal": 1,
        "amount": round(float(stake), 2),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": currency,
        "duration": int(duration),
        "duration_unit": duration_unit,
        sym_key: symbol,
    }


def instruments_from_active_symbols(resp: Dict[str, Any]) -> List[Instrument]:
    return [normalize_active_symbol(d) for d in (resp.get("active_symbols") or [])]


def volatility_instruments(items: Iterable[Instrument]) -> List[Instrument]:
    return [i for i in items if is_volatility_index(i)]
