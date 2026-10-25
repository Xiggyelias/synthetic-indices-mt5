from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from mt5_connection import MT5Connection
from trade_manager import TradeManager
from telegram_notifier import TelegramNotifier
from trading_bot_engulfing_v2 import TradingConfig


class TradeSupervisor:
    def __init__(
        self,
        connection: MT5Connection,
        trade_manager: TradeManager,
        magic: int,
        timeframe: str = "H1",
        notifier: Optional[TelegramNotifier] = None,
        on_trade_closed=None,
        config: Optional[TradingConfig] = None,
    ) -> None:
        self.connection = connection
        self.trade_manager = trade_manager
        self.magic = int(magic)
        self.timeframe = str(timeframe or "H1").upper()
        self.notifier = notifier
        self.logger = logging.getLogger("trade-supervisor")
        self.on_trade_closed = on_trade_closed  # Callback: on_trade_closed(win: bool)
        self.config = config

    def sync_with_terminal(self) -> None:
        positions = self.connection.get_positions()
        if positions is None:
            return

        # adopt any MT5 positions with our magic that aren't in local state
        for p in positions:
            if int(getattr(p, "magic", 0) or 0) != self.magic:
                continue
            ticket = int(getattr(p, "ticket", 0) or getattr(p, "identifier", 0) or 0)
            if ticket <= 0:
                continue
            if self.trade_manager.get(ticket) is None:
                adopted = self.trade_manager.adopt_position(p)
                self.logger.info("Adopted position from terminal: %s", adopted.ticket)
                if self.notifier:
                    self.notifier.send(f"Adopted open position: {adopted.symbol} {adopted.direction.upper()} ticket={adopted.ticket}")

        # detect closed trades (present in state, missing in terminal)
        open_tickets_terminal = {int(getattr(p, "ticket", 0) or getattr(p, "identifier", 0) or 0) for p in positions}
        for ticket, t in list(self.trade_manager.open_trades.items()):
            if t.magic != self.magic:
                continue
            if int(ticket) not in open_tickets_terminal:
                self.logger.info("Detected closed position ticket=%s", ticket)
                closed_at = datetime.now(timezone.utc).isoformat()

                # ── Fetch costs ONCE ──────────────────────────────────────────────
                comm, swap = self.connection.calculate_position_costs(ticket)
                entry      = float(t.entry_price or 0.0)
                vol        = float(t.volume or 1.0)
                net_pnl    = float(t.profit_loss or 0.0) + comm + swap

                # ── Win/loss: prefer actual PnL; fall back to SL position ─────────
                if abs(net_pnl) >= 0.01:
                    is_win = net_pnl > 0
                else:
                    last_sl = float(t.last_known_sl or t.stop_loss or 0.0)
                    if entry > 0 and last_sl > 0:
                        is_win = (last_sl >= entry) if t.direction == "buy" else (last_sl <= entry)
                    else:
                        is_win = False

                # ── R-multiple ────────────────────────────────────────────────────
                r_achieved  = 0.0
                initial_sl  = float(getattr(t, "stop_loss", 0.0) or 0.0)
                risk_per_unit = abs(entry - initial_sl)
                if risk_per_unit > 0 and vol > 0:
                    r_achieved = net_pnl / (risk_per_unit * vol)

                # ── Telegram notification ─────────────────────────────────────────
                if self.notifier:
                    pnl_emoji   = "💰" if is_win else "📉"
                    result_text = "WIN"  if is_win else "LOSS"
                    self.notifier.send(
                        f"{pnl_emoji} Position closed ({result_text}): "
                        f"{t.symbol} {t.direction.upper()} ticket={t.ticket}\n"
                        f"Net PnL: ${net_pnl:+.2f} | R: {r_achieved:+.2f}R "
                        f"(Comm: ${comm:.2f}, Swap: ${swap:.2f})"
                    )

                # ── Performance callback ──────────────────────────────────────────
                if self.on_trade_closed:
                    try:
                        trade_data = {
                            "ticket":           t.ticket,
                            "symbol":           t.symbol,
                            "direction":        t.direction,
                            "entry_price":      entry,
                            "exit_price":       0.0,
                            "volume":           vol,
                            "pnl":              net_pnl,
                            "r_achieved":       r_achieved,
                            "confluence_score": int(getattr(t, "confluence_score", 0) or 0),
                            "session":          str(getattr(t, "session", "Unknown") or "Unknown"),
                            "opened_at":        str(t.opened_at or ""),
                            "closed_at":        closed_at,
                            "win":              is_win,
                            "commission":       comm,
                            "swap":             swap,
                            "confluence_signals": getattr(t, "confluence_signals", {}) or {},
                        }
                        self.on_trade_closed(is_win, trade_data)
                    except Exception:
                        pass

                # ── Remove from open trades ───────────────────────────────────────
                self.trade_manager.remove(ticket, closed_snapshot={
                    "closed_at":  closed_at,
                    "commission": comm,
                    "swap":       swap,
                    "r_achieved": r_achieved,
                })

    def manage_position(self, ticket: int) -> None:
        t = self.trade_manager.get(ticket)
        if t is None or t.magic != self.magic:
            return

        pos = self.connection.get_position(ticket)
        if pos is None:
            return

        symbol = str(getattr(pos, "symbol", ""))
        tick = self.connection.get_symbol_price(symbol)
        if tick is None:
            return

        bid = float(tick.bid)
        ask = float(tick.ask)

        current_sl = float(getattr(pos, "sl", 0.0) or 0.0)
        current_tp = float(getattr(pos, "tp", 0.0) or 0.0)

        rates = self.connection.get_rates(symbol, timeframe=self.timeframe, count=15)
        if rates is None or len(rates) < 3:
            return

        last_closed = rates[-2]
        last_closed_close = float(last_closed["close"])
        last_closed_low = float(last_closed["low"])
        last_closed_high = float(last_closed["high"])
        last_closed_time = int(last_closed["time"])
        pos_time = int(getattr(pos, "time", 0) or 0)
        if pos_time and last_closed_time <= pos_time:
            return
        if int(t.last_trail_time or 0) >= last_closed_time:
            return

        # Need an initial SL to compute R
        initial_sl = float(t.stop_loss or 0.0)
        entry = float(t.entry_price or 0.0)
        if initial_sl <= 0 or entry <= 0:
            return

        r = abs(entry - initial_sl)
        if r <= 0:
            return

        rules = self.connection.get_broker_rules(symbol)
        if rules is None:
            return

        point = float(getattr(rules, "point", 0.0) or 0.0)
        min_step = (point * 0.5) if point > 0 else 1e-9

        previous_sl = float(current_sl if current_sl > 0 else initial_sl)
        desired_sl = float(previous_sl)
        current_tp_value = current_tp or None
        be_reached = False
        trailing_reached = False

        if t.direction == "buy":
            be_trigger = entry + r
            trailing_trigger = entry + (1.5 * r)
            trailing_floor = entry + (0.5 * r)

            if last_closed_close >= be_trigger:
                be_reached = True
                desired_sl = max(desired_sl, entry)

            if last_closed_close >= trailing_trigger:
                trailing_reached = True
                # After partial close, use swing-based trailing
                if t.partial_closed:
                    swing_low = self._find_swing_point(rates, "buy", lookback=10)
                    if swing_low and swing_low > entry:
                        desired_sl = max(desired_sl, swing_low)
                    else:
                        desired_sl = max(desired_sl, trailing_floor)
                else:
                    desired_sl = max(desired_sl, trailing_floor)

            if t.moved_to_break_even and not be_reached and not trailing_reached:
                desired_sl = max(desired_sl, last_closed_low, entry)

            if desired_sl >= bid:
                return

            should_update = desired_sl > (current_sl + min_step)
            price_for_validation = bid
        else:
            be_trigger = entry - r
            trailing_trigger = entry - (1.5 * r)
            trailing_floor = entry - (0.5 * r)

            if last_closed_close <= be_trigger:
                be_reached = True
                desired_sl = min(desired_sl, entry)

            if last_closed_close <= trailing_trigger:
                trailing_reached = True
                # After partial close, use swing-based trailing
                if t.partial_closed:
                    swing_high = self._find_swing_point(rates, "sell", lookback=10)
                    if swing_high and swing_high < entry:
                        desired_sl = min(desired_sl, swing_high)
                    else:
                        desired_sl = min(desired_sl, trailing_floor)
                else:
                    desired_sl = min(desired_sl, trailing_floor)

            if t.moved_to_break_even and not be_reached and not trailing_reached:
                desired_sl = min(desired_sl, last_closed_high, entry)

            if desired_sl <= ask:
                return

            should_update = (current_sl <= 0) or (desired_sl < (current_sl - min_step))
            price_for_validation = ask

        # Stagnation / Time-decay stop: if open for >= stagnation_bars without reaching 1R, protect capital
        stagnation_bars = int(getattr(self.config, 'stagnation_bars', 10) or 10) if self.config is not None else 10
        stagnation_reached = False
        if stagnation_bars > 0 and pos_time > 0 and not be_reached and not trailing_reached and not t.moved_to_break_even:
            elapsed_bars = sum(1 for r in rates if int(r.get("time", 0)) > pos_time)
            if elapsed_bars >= stagnation_bars:
                if t.direction == "buy" and bid > entry:
                    desired_sl = max(desired_sl, entry)
                    stagnation_reached = True
                    should_update = desired_sl > (current_sl + min_step)
                elif t.direction == "sell" and ask < entry:
                    desired_sl = min(desired_sl, entry)
                    stagnation_reached = True
                    should_update = (current_sl <= 0) or (desired_sl < (current_sl - min_step))

        # Partial close at 1.5R: scale out 50% of position
        allow_pc = True
        if self.config is not None:
            allow_pc = getattr(self.config, 'allow_partial_close', True)

        partial_close_done = bool(getattr(t, 'partial_closed', False))
        if allow_pc and not partial_close_done and trailing_reached:
            try:
                current_vol = float(getattr(pos, 'volume', 0.0) or 0.0)
                close_vol = self.connection.normalize_volume(symbol, current_vol * 0.5)
                if close_vol is not None and close_vol > 0 and close_vol < current_vol:
                    max_slip = 10
                    if self.config is not None:
                        max_slip = int(getattr(self.config, 'max_slippage_points', 10) or 10)
                    partial_result = self.connection.close_position(
                        ticket=ticket,
                        deviation=max_slip,
                        comment="PARTIAL@1.5R",
                        volume=close_vol,
                    )
                    if partial_result is not None:
                        t.partial_closed = True
                        t.partial_close_size = float(close_vol)
                        self.trade_manager.upsert(t)
                        self.logger.info("Partial close 50%% at 1.5R: ticket=%s vol=%.2f", ticket, close_vol)
                        if self.notifier:
                            self.notifier.send(f"Scaled out 50% at 1.5R: {symbol} ticket={ticket} vol={close_vol:.2f}")
            except Exception as exc:
                self.logger.warning("Partial close failed ticket=%s: %s", ticket, exc)

        if not should_update:
            return

        ok, _ = self.connection.validate_stops(
            symbol,
            price_for_validation,
            desired_sl,
            current_tp_value,
            direction=t.direction,
        )
        if not ok:
            return

        comment = "TRAIL"
        if trailing_reached:
            comment = "TRAIL@1.5R"
        elif be_reached and not t.moved_to_break_even:
            comment = "BE@1R"
        elif stagnation_reached:
            comment = "STAGNATION@BE"

        res = self.connection.set_position_sltp(
            ticket=ticket,
            sl=desired_sl,
            tp=current_tp,
            comment=comment,
        )
        if res is None:
            return

        t.moved_to_break_even = bool(t.moved_to_break_even or be_reached or stagnation_reached)
        t.last_known_sl = float(desired_sl)
        t.last_known_tp = float(current_tp)
        t.last_trail_time = int(last_closed_time)
        # Keep floating P&L current so sync_with_terminal has an accurate value
        t.profit_loss = float(getattr(pos, "profit", 0.0) or 0.0)
        self.trade_manager.upsert(t)

        action = "breakeven" if comment == "BE@1R" else ("stagnation_be" if comment == "STAGNATION@BE" else "trailing")

        self.logger.info("Updated managed SL ticket=%s action=%s sl=%.5f", ticket, action, desired_sl)
        if self.notifier:
            self.notifier.send(f"Updated SL ({action}): {symbol} ticket={ticket} SL={desired_sl}")

    def check_structure_invalidation(self, ticket: int) -> None:
        """Check if market structure has broken against the trade, triggering early exit."""
        t = self.trade_manager.get(ticket)
        if t is None or t.magic != self.magic:
            return

        symbol = str(t.symbol or "")
        if not symbol:
            return

        rates = self.connection.get_rates(symbol, timeframe=self.timeframe, count=10)
        if rates is None or len(rates) < 5:
            return

        entry = float(t.entry_price or 0.0)
        if entry <= 0:
            return

        # Check for structure break: only on fully closed candles (rates[-2]), not the forming candle (rates[-1])
        closed_rates = rates[:-1]
        if len(closed_rates) < 4:
            return

        last_closed = closed_rates[-1]
        last_closed_time = int(last_closed.get("time", 0) or 0)
        if int(getattr(t, "last_structure_check_time", 0) or 0) >= last_closed_time:
            return

        recent = closed_rates[-5:]
        max_slip = 10
        if self.config is not None:
            max_slip = int(getattr(self.config, 'max_slippage_points', 10) or 10)

        if t.direction == "buy":
            # For buys, structure breaks if closed candle is below recent swing low
            swing_low = float(min(r["low"] for r in recent[:-1]))
            last_close = float(last_closed["close"])
            if last_close < swing_low:
                self.logger.warning("Structure invalidation: closing BUY ticket=%s close=%.5f < swing_low=%.5f", ticket, last_close, swing_low)
                if self.notifier:
                    self.notifier.send(f"⚠️ Structure break! Early exit: {symbol} BUY ticket={ticket}")
                self.connection.close_position(ticket=ticket, deviation=max_slip, comment="STRUCTURE_BREAK")
                t.last_structure_check_time = last_closed_time
                self.trade_manager.upsert(t)
        else:
            # For sells, structure breaks if closed candle is above recent swing high
            swing_high = float(max(r["high"] for r in recent[:-1]))
            last_close = float(last_closed["close"])
            if last_close > swing_high:
                self.logger.warning("Structure invalidation: closing SELL ticket=%s close=%.5f > swing_high=%.5f", ticket, last_close, swing_high)
                if self.notifier:
                    self.notifier.send(f"⚠️ Structure break! Early exit: {symbol} SELL ticket={ticket}")
                self.connection.close_position(ticket=ticket, deviation=max_slip, comment="STRUCTURE_BREAK")
                t.last_structure_check_time = last_closed_time
                self.trade_manager.upsert(t)

    def _find_swing_point(self, rates, direction: str, lookback: int = 10) -> Optional[float]:
        """
        Find the most recent swing low (for buys) or swing high (for sells).
        Used for swing-based trailing stop after partial close.
        """
        if rates is None or len(rates) < lookback:
            return None
        
        recent = rates[-lookback:]
        
        if direction == "buy":
            # Find swing low: lowest low with higher lows on both sides
            lows = [float(r["low"]) for r in recent]
            if len(lows) < 3:
                return min(lows) if lows else None
            
            # Simple approach: find the lowest low in the lookback period
            # More sophisticated: find fractal swing low
            for i in range(len(recent) - 2, 0, -1):
                if recent[i]["low"] < recent[i-1]["low"] and recent[i]["low"] < recent[i+1]["low"]:
                    return float(recent[i]["low"])
            
            # Fallback to lowest low
            return min(lows)
        else:
            # Find swing high: highest high with lower highs on both sides
            highs = [float(r["high"]) for r in recent]
            if len(highs) < 3:
                return max(highs) if highs else None
            
            # Find fractal swing high
            for i in range(len(recent) - 2, 0, -1):
                if recent[i]["high"] > recent[i-1]["high"] and recent[i]["high"] > recent[i+1]["high"]:
                    return float(recent[i]["high"])
            
            # Fallback to highest high
            return max(highs)

    def manage_all(self, allow_modify: bool = True) -> None:
        self.sync_with_terminal()
        if not allow_modify:
            return
        for ticket, t in list(self.trade_manager.open_trades.items()):
            if t.magic != self.magic:
                continue
            self.manage_position(int(ticket))
            self.check_structure_invalidation(int(ticket))
