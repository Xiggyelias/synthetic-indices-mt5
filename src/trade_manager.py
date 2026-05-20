from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass
class ManagedTrade:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    magic: int
    comment: str = ""
    opened_at: str = ""
    moved_to_break_even: bool = False
    last_known_sl: float = 0.0
    last_known_tp: float = 0.0
    last_trail_time: int = 0
    partial_closed: bool = False
    partial_close_size: float = 0.0
    # Enhanced logging fields
    setup_type: str = "pullback"  # breakout, pullback, reversal
    score: int = 0
    planned_rr: float = 0.0
    r_achieved: float = 0.0
    what_went_right: str = ""
    what_went_wrong: str = ""
    take_again: str = "Yes" # Yes/No/Modified
    regime: str = ""
    confluence_signals: str = ""
    confluence_score: int = 0
    session: str = "Unknown"
    profit_loss: float = 0.0  # Final PnL when closed


class TradeManager:
    def __init__(self, state_path: str = "trades_state.json") -> None:
        self.state_path = state_path
        self.open_trades: Dict[int, ManagedTrade] = {}
        self.meta: dict = {}
        self.closed_trades: List[dict] = []

    def load(self) -> None:
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.meta = (raw or {}).get("meta", {}) or {}
        open_raw = (raw or {}).get("open_trades", {}) or {}
        self.open_trades = {}
        for k, v in open_raw.items():
            try:
                ticket = int(v.get("ticket") or k)
                self.open_trades[ticket] = ManagedTrade(**{**v, "ticket": ticket})
            except Exception:
                continue
        self.closed_trades = (raw or {}).get("closed_trades", []) or []

    def save(self) -> None:
        payload = {
            "updated_at": int(time.time()),
            "meta": self.meta,
            "open_trades": {str(k): asdict(v) for k, v in self.open_trades.items()},
            "closed_trades": self.closed_trades,
        }
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.state_path)

    def get(self, ticket: int) -> Optional[ManagedTrade]:
        return self.open_trades.get(int(ticket))

    def upsert(self, trade: ManagedTrade) -> None:
        self.open_trades[int(trade.ticket)] = trade
        self.save()

    def remove(self, ticket: int, closed_snapshot: Optional[dict] = None) -> None:
        ticket_i = int(ticket)
        t = self.open_trades.pop(ticket_i, None)
        if closed_snapshot is None:
            closed_snapshot = {}
        if t is not None:
            self.closed_trades.append({**asdict(t), **closed_snapshot})
        self.save()

    def adopt_position(self, pos) -> ManagedTrade:
        ticket = int(getattr(pos, "ticket", 0) or getattr(pos, "identifier", 0) or 0)
        symbol = str(getattr(pos, "symbol", ""))
        volume = float(getattr(pos, "volume", 0.0) or 0.0)
        price_open = float(getattr(pos, "price_open", 0.0) or 0.0)
        sl = float(getattr(pos, "sl", 0.0) or 0.0)
        tp = float(getattr(pos, "tp", 0.0) or 0.0)
        magic = int(getattr(pos, "magic", 0) or 0)
        comment = str(getattr(pos, "comment", "") or "")
        direction = "buy" if int(getattr(pos, "type", 0) or 0) == 0 else "sell"
        opened_at = str(getattr(pos, "time", "") or "")
        trade = ManagedTrade(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            volume=volume,
            entry_price=price_open,
            stop_loss=sl,
            take_profit=tp,
            magic=magic,
            comment=comment,
            opened_at=opened_at,
            moved_to_break_even=False,
            last_known_sl=sl,
            last_known_tp=tp,
        )
        self.open_trades[ticket] = trade
        self.save()
        return trade
