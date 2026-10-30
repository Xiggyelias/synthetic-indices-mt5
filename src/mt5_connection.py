"""
Reusable MetaTrader 5 connection helper with auto-reconnect, retries,
and broker rule validation (volume steps, stop levels).

Requirements:
    pip install MetaTrader5

Important:
    - MT5 terminal must be installed and logged in at least once.
    - Use demo accounts by default; switch to live only when ready.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


# --- Data models ---

@dataclass
class MT5Credentials:
    login: int = 0
    password: str = ""
    server: str = "Deriv-Demo"
    terminal_path: Optional[str] = None
    is_demo: bool = True  # default to demo
    path: Optional[str] = None
    portable: bool = False

    def __post_init__(self):
        if self.path and not self.terminal_path:
            self.terminal_path = self.path
        elif self.terminal_path and not self.path:
            self.path = self.terminal_path


@dataclass
class SymbolPrice:
    symbol: str
    bid: float
    ask: float
    time: float


@dataclass
class BrokerRules:
    volume_min: float
    volume_max: float
    volume_step: float
    stop_level_points: int
    point: float
    digits: int = 0
    tick_size: float = 0.0
    tick_value: float = 0.0
    freeze_level_points: int = 0


# --- Timeframe mapping helpers ---

TIMEFRAME_MAP: Dict[str, int] = {
    "M1": getattr(mt5, "TIMEFRAME_M1", 1) if mt5 else 1,
    "M5": getattr(mt5, "TIMEFRAME_M5", 5) if mt5 else 5,
    "M15": getattr(mt5, "TIMEFRAME_M15", 15) if mt5 else 15,
    "M30": getattr(mt5, "TIMEFRAME_M30", 30) if mt5 else 30,
    "H1": getattr(mt5, "TIMEFRAME_H1", 16385) if mt5 else 16385,
    "H4": getattr(mt5, "TIMEFRAME_H4", 16388) if mt5 else 16388,
    "D1": getattr(mt5, "TIMEFRAME_D1", 16408) if mt5 else 16408,
}


# --- Connection wrapper ---

class MT5Connection:
    """
    Thin wrapper around MetaTrader5 with retries, auto-reconnect,
    and broker constraint helpers. Separated from strategy logic.
    """

    def __init__(
        self,
        credentials: MT5Credentials,
        app_name: str = "mt5-trading-bot",
        max_retries: int = 3,
        retry_delay: float = 2.0,
        auto_reconnect: bool = True,
        initialize_timeout_ms: int = 30000,
    ):
        self.credentials = credentials
        self.app_name = app_name
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.auto_reconnect = auto_reconnect
        self.initialize_timeout_ms = initialize_timeout_ms
        self.connected: bool = False
        self.last_order_error: Dict[str, Any] = {}

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        self.logger = logging.getLogger(app_name)

    # --- Lifecycle ---

    def initialize(self) -> bool:
        """Initialize MT5 connection with retries."""
        if mt5 is None:
            self.logger.warning("MetaTrader5 package is unavailable in this environment (non-Windows / Docker).")
            return False
        self.logger.info(
            "Initializing MT5 (%s)... demo=%s",
            self.app_name,
            self.credentials.is_demo,
        )
        if not self._connect_with_retries():
            return False
        if not self._wait_for_terminal_connected():
            self.logger.error("MT5 terminal is not connected (no green bars).")
            self.shutdown()
            return False
        return True

    def shutdown(self) -> None:
        """Shutdown MT5 session."""
        if self.connected:
            mt5.shutdown()
            self.connected = False
            self.logger.info("MT5 shutdown complete.")

    def _connect_with_retries(self) -> bool:
        if not self.credentials.password:
            return self._try_attach_to_running_terminal()

        for attempt in range(1, self.max_retries + 1):
            try:
                mt5.shutdown()  # ensure clean state
                init_kwargs = {
                    "login": self.credentials.login,
                    "password": self.credentials.password,
                    "server": self.credentials.server,
                    "timeout": self.initialize_timeout_ms,
                }
                if self.credentials.terminal_path:
                    init_kwargs["path"] = self.credentials.terminal_path

                if mt5.initialize(**init_kwargs):
                    account_info = mt5.account_info()
                    if account_info:
                        self._log_account_status(account_info)
                        if not account_info.trade_allowed:
                            self.logger.warning(
                                "Trading is disabled on this account. Data access only until broker enables trading."
                            )
                        if self.credentials.is_demo and account_info.trade_mode not in (
                            mt5.ACCOUNT_TRADE_MODE_DEMO,
                            mt5.ACCOUNT_TRADE_MODE_CONTEST,
                        ):
                            self.logger.warning(
                                "Credentials flagged as demo but broker reports trade mode=%s.",
                                account_info.trade_mode,
                            )
                        if not self.credentials.is_demo and account_info.trade_mode in (
                            mt5.ACCOUNT_TRADE_MODE_DEMO,
                            mt5.ACCOUNT_TRADE_MODE_CONTEST,
                        ):
                            self.logger.warning(
                                "Credentials flagged as live but broker reports trade mode=%s.",
                                account_info.trade_mode,
                            )
                        self.connected = True
                        self.logger.info(
                            "MT5 connected: login=%s | balance=%.2f | equity=%.2f",
                            account_info.login,
                            account_info.balance,
                            account_info.equity,
                        )
                        return True
                    self.logger.error("Connected but failed to read account info.")
                else:
                    code, msg = mt5.last_error()
                    self.logger.error(
                        "MT5 initialize failed (attempt %s/%s): [%s] %s",
                        attempt,
                        self.max_retries,
                        code,
                        msg,
                    )

                    if code == -6:
                        self._log_authorization_troubleshooting(code, msg)

                    if code == -6:
                        if self._try_attach_to_running_terminal():
                            return True
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.exception("MT5 initialize exception: %s", exc)

            time.sleep(self.retry_delay)

        self.logger.error("Exceeded max retries. MT5 connection not established.")
        return False

    def _log_authorization_troubleshooting(self, code: int, msg: str) -> None:
        if code != -6:
            return
        self.logger.error(
            "Authorization failed. Ensure MT5 is opened from '%s', logged in to login=%s on server='%s', and shows green bars. Attach mode works only after the terminal is already authorized.",
            self.credentials.terminal_path or "",
            self.credentials.login,
            self.credentials.server,
        )

    def ensure_connected(self) -> bool:
        """
        Ensure the session is alive. Attempts auto-reconnect if enabled.
        """
        if not self.connected or mt5.account_info() is None:
            self.logger.warning("MT5 session appears down. Attempting reconnect...")
            self.connected = False
            if self.auto_reconnect:
                return self._connect_with_retries()
            return False
        return True

    # --- Info helpers ---

    def get_account_info(self) -> Optional[mt5.AccountInfo]:
        if not self.ensure_connected():
            return None
        return mt5.account_info()

    def get_account_summary(self) -> Optional[Dict[str, Union[int, float, str]]]:
        info = self.get_account_info()
        if info is None:
            return None
        return {
            "login": info.login,
            "name": info.name,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "currency": info.currency,
            "server": info.server,
            "trade_mode": info.trade_mode,
            "trade_allowed": info.trade_allowed,
        }

    def get_terminal_info(self):
        if not self.ensure_connected():
            return None
        return mt5.terminal_info()

    def is_terminal_connected(self) -> Optional[bool]:
        terminal = self.get_terminal_info()
        if terminal is None:
            return None
        return bool(getattr(terminal, "connected", None))

    def get_connection_status(self) -> Dict[str, Union[bool, str, int, None]]:
        connected = self.is_terminal_connected()
        terminal = self.get_terminal_info()
        trade_allowed = None
        trade_expert = None
        if terminal is not None:
            trade_allowed = getattr(terminal, "trade_allowed", None)
            trade_expert = getattr(terminal, "trade_expert", None)
        return {
            "connected": connected,
            "trade_allowed": trade_allowed,
            "trade_expert": trade_expert,
            "terminal_path": self.credentials.terminal_path or "",
        }

    def get_tick(self, symbol: str) -> Optional[Any]:
        """Fetch current tick for symbol."""
        if not self.ensure_connected():
            return None
        return mt5.symbol_info_tick(str(symbol))

    def get_positions(self, symbol: Optional[str] = None):
        if not self.ensure_connected():
            return None
        if symbol:
            return mt5.positions_get(symbol=symbol)
        return mt5.positions_get()

    def get_position(self, ticket: int, symbol: str = ""):
        if not self.ensure_connected():
            return None
        positions = mt5.positions_get(ticket=int(ticket))
        if positions is not None and len(positions) > 0:
            return positions[0]
        if symbol:
            positions_by_symbol = mt5.positions_get(symbol=str(symbol))
            if positions_by_symbol is not None and len(positions_by_symbol) == 1:
                return positions_by_symbol[0]
        return None

    def get_position_history(self, ticket: int) -> List[mt5.TradeDeal]:
        """Fetch all deals associated with a position ticket."""
        if not self.ensure_connected():
            return []
        deals = mt5.history_deals_get(position=int(ticket))
        return list(deals) if deals else []

    def calculate_position_costs(self, ticket: int) -> Tuple[float, float]:
        """Calculate total commission and swap for a position."""
        deals = self.get_position_history(ticket)
        comm = sum(float(getattr(d, "commission", 0.0)) for d in deals)
        swap = sum(float(getattr(d, "swap", 0.0)) for d in deals)
        return comm, swap

    def find_recent_position(
        self,
        symbol: str,
        direction: str,
        volume: float,
        magic: int = 0,
        comment: str = "",
    ):
        if not self.ensure_connected():
            return None
        positions = mt5.positions_get(symbol=str(symbol))
        if positions is None or len(positions) == 0:
            return None

        position_type_buy = getattr(mt5, "POSITION_TYPE_BUY", 0)
        position_type_sell = getattr(mt5, "POSITION_TYPE_SELL", 1)
        want_buy = direction.lower() == "buy"
        want_type = position_type_buy if want_buy else position_type_sell

        candidates = []
        for p in positions:
            try:
                if int(getattr(p, "type", -1)) != int(want_type):
                    continue
                if abs(float(getattr(p, "volume", 0.0)) - float(volume)) > 1e-9:
                    continue
                if int(getattr(p, "magic", 0)) != int(magic):
                    continue
                if comment and str(getattr(p, "comment", "")) != str(comment):
                    continue
                candidates.append(p)
            except Exception:
                continue

        if not candidates:
            return None

        def _pos_time(x):
            return int(getattr(x, "time_update", 0) or getattr(x, "time", 0) or 0)

        candidates.sort(key=_pos_time, reverse=True)
        return candidates[0]

    def set_position_sltp(
        self,
        ticket: int,
        sl: float,
        tp: float,
        comment: str = "",
    ) -> Optional[mt5.TradeResult]:
        if not self.ensure_connected():
            return None

        pos = self.get_position(ticket)
        if pos is None:
            self.logger.error("Position not found for SL/TP update ticket=%s", ticket)
            return None

        symbol = getattr(pos, "symbol", None)
        if not symbol:
            self.logger.error("Position missing symbol for SL/TP update ticket=%s", ticket)
            return None

        info = self.ensure_symbol(str(symbol))
        if info is None:
            return None

        tick = mt5.symbol_info_tick(str(symbol))
        if tick is None:
            self.logger.error("No tick data for %s; cannot validate SL/TP", symbol)
            return None

        position_type_buy = getattr(mt5, "POSITION_TYPE_BUY", 0)
        is_buy = bool(getattr(pos, "type", None) == position_type_buy)
        side = "buy" if is_buy else "sell"
        sl, tp = self._normalize_order_stops(str(symbol), side, sl if sl else None, tp if tp else None, info)
        price_for_validation = tick.bid if is_buy else tick.ask
        price_for_validation = self._normalize_price_for_symbol(str(symbol), price_for_validation, info=info)

        ok, err = self.validate_stops(str(symbol), price_for_validation, sl or None, tp or None, direction=side)
        if not ok:
            self.logger.debug("SL/TP validation rejected for %s: %s", symbol, err)
            return None

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "symbol": str(symbol),
            "sl": float(sl),
            "tp": float(tp),
            "magic": int(getattr(pos, "magic", 0)),
            "comment": comment,
        }

        result = mt5.order_send(request)
        if result is None:
            code, msg = mt5.last_error()
            self.logger.error("SL/TP update returned None for %s: [%s] %s", symbol, code, msg)
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.error(
                "SL/TP update rejected for %s | retcode=%s | comment=%s",
                symbol,
                result.retcode,
                result.comment,
            )
            return None

        self.logger.info("SL/TP updated for %s | ticket=%s | sl=%.5f tp=%.5f", symbol, ticket, sl, tp)
        return result

    def close_position(
        self,
        ticket: int,
        deviation: int = 10,
        comment: str = "",
        volume: Optional[float] = None,
    ) -> Optional[mt5.TradeResult]:
        if not self.ensure_connected():
            return None

        positions = mt5.positions_get(ticket=ticket)
        if positions is None or len(positions) == 0:
            self.logger.error("Position not found for ticket=%s", ticket)
            return None
        pos = positions[0]

        symbol = getattr(pos, "symbol", None)
        if not symbol:
            self.logger.error("Position missing symbol for ticket=%s", ticket)
            return None

        info = self.ensure_symbol(symbol)
        if info is None:
            return None

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            self.logger.error("No tick data for %s; cannot close position", symbol)
            return None

        position_type_buy = getattr(mt5, "POSITION_TYPE_BUY", 0)
        is_buy = bool(getattr(pos, "type", None) == position_type_buy)
        order_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        price = tick.bid if is_buy else tick.ask
        price = self._normalize_price_for_symbol(symbol, price, info=info)

        close_vol = float(volume) if volume is not None else float(getattr(pos, "volume", 0.0))

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(ticket),
            "symbol": symbol,
            "volume": close_vol,
            "type": order_type,
            "price": price,
            "deviation": deviation,
            "magic": int(getattr(pos, "magic", 0)),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._select_filling_mode(info),
        }

        invalid_fill_retcode = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", None)
        tried_modes = []
        for mode in [
            request["type_filling"],
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]:
            if mode in tried_modes:
                continue
            tried_modes.append(mode)
            request["type_filling"] = mode
            result = mt5.order_send(request)
            if result is None:
                code, msg = mt5.last_error()
                self.logger.error("Close send returned None for %s: [%s] %s", symbol, code, msg)
                return None
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                self.logger.info(
                    "Position closed: %s %.2f %s at %.5f | ticket=%s",
                    "SELL" if is_buy else "BUY",
                    float(getattr(pos, "volume", 0.0)),
                    symbol,
                    price,
                    ticket,
                )
                return result

            is_invalid_fill = invalid_fill_retcode is not None and result.retcode == invalid_fill_retcode
            if is_invalid_fill or ("Unsupported filling mode" in (result.comment or "")):
                self.logger.warning(
                    "Close rejected for %s due to filling mode; trying next | retcode=%s | comment=%s",
                    symbol,
                    result.retcode,
                    result.comment,
                )
                continue

            self.logger.error(
                "Close rejected for %s | retcode=%s | comment=%s",
                symbol,
                result.retcode,
                result.comment,
            )
            return None

        self.logger.error("Close rejected for %s: no supported filling mode found.", symbol)
        return None

    def close_all_positions(
        self,
        symbol: str,
        deviation: int = 10,
        comment: str = "",
    ) -> int:
        """Close every open position on *symbol*. Returns the number of successful closes."""
        if not self.ensure_connected():
            return 0

        positions = mt5.positions_get(symbol=str(symbol))
        if positions is None or len(positions) == 0:
            return 0

        tickets: list[int] = []
        for pos in positions:
            ticket = int(getattr(pos, "ticket", 0) or getattr(pos, "identifier", 0) or 0)
            if ticket > 0:
                tickets.append(ticket)

        closed = 0
        for ticket in tickets:
            result = self.close_position(ticket=ticket, deviation=deviation, comment=comment)
            if result is not None:
                closed += 1

        return closed

    @staticmethod
    def last_error() -> Tuple[int, str]:
        return mt5.last_error()

    def get_last_order_error(self) -> Dict[str, Any]:
        return dict(self.last_order_error or {})

    def is_symbol_tradable_now(
        self,
        symbol: str,
        direction: str = "",
        max_tick_age_seconds: int = 1800,
    ) -> Tuple[bool, str]:
        """Return whether a new market order can be sent for this symbol now."""
        now_utc = datetime.now(timezone.utc)
        weekday = now_utc.weekday()  # Monday=0, Sunday=6
        if weekday == 5:
            return False, "weekly market close: Saturday"
        if weekday == 6 and now_utc.hour < 21:
            return False, "weekly market close: before Sunday 21:00 UTC open"
        if weekday == 4 and now_utc.hour >= 21:
            return False, "weekly market close: after Friday 21:00 UTC close"

        info = self.ensure_symbol(symbol)
        if info is None:
            return False, f"{symbol} is not available in MT5"

        try:
            trade_mode = int(getattr(info, "trade_mode", -1))
        except Exception:
            trade_mode = -1
        disabled = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
        close_only = getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3)
        long_only = getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", 1)
        short_only = getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", 2)

        if trade_mode == disabled:
            return False, f"{symbol} trading is disabled by the broker"
        if trade_mode == close_only:
            return False, f"{symbol} is close-only at the broker"

        side = str(direction or "").lower()
        if side == "buy" and trade_mode == short_only:
            return False, f"{symbol} is short-only at the broker"
        if side == "sell" and trade_mode == long_only:
            return False, f"{symbol} is long-only at the broker"

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False, f"{symbol} has no live tick"

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0:
            return False, f"{symbol} quote is invalid: bid={bid} ask={ask}"

        tick_time = float(getattr(tick, "time", 0.0) or 0.0)
        if tick_time <= 0:
            return False, f"{symbol} quote time is unavailable"

        tick_age = max(0.0, now_utc.timestamp() - tick_time)
        if tick_age > max(1, int(max_tick_age_seconds)):
            return False, f"{symbol} market appears closed: last quote is {int(tick_age)}s old"

        return True, ""

    def is_trading_allowed(self) -> bool:
        info = self.get_account_info()
        return bool(info and info.trade_allowed)

    # --- Symbol helpers ---

    def ensure_symbol(self, symbol: str) -> Optional[mt5.SymbolInfo]:
        if not self.ensure_connected():
            return None
        info = mt5.symbol_info(symbol)
        if info is None:
            self.logger.error("Symbol %s not found.", symbol)
            return None
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                self.logger.error("Failed to select symbol %s", symbol)
                return None
        return info

    def get_symbol_price(self, symbol: str) -> Optional[SymbolPrice]:
        info = self.ensure_symbol(symbol)
        if info is None:
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            self.logger.error("Failed to get tick for %s", symbol)
            return None
        return SymbolPrice(symbol=symbol, bid=tick.bid, ask=tick.ask, time=tick.time)

    def get_rates(
        self,
        symbol: str,
        timeframe: str = "H1",
        count: int = 200,
    ):
        """
        Retrieve OHLCV rates as a list of dicts (compatible with pandas.DataFrame).
        """
        info = self.ensure_symbol(symbol)
        if info is None:
            return None
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'. Use one of {list(TIMEFRAME_MAP.keys())}")
        if not self.ensure_connected():
            return None
        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME_MAP[timeframe], 0, count)
        if rates is None:
            code, msg = mt5.last_error()
            self.logger.error("Failed to fetch rates for %s: [%s] %s", symbol, code, msg)
            return None
        return rates

    def get_rates_range(
        self,
        symbol: str,
        timeframe: str,
        date_from: datetime,
        date_to: datetime,
    ):
        """
        Retrieve OHLCV rates between two datetimes as a list of dicts.
        """
        info = self.ensure_symbol(symbol)
        if info is None:
            return None
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'. Use one of {list(TIMEFRAME_MAP.keys())}")
        if not self.ensure_connected():
            return None
        rates = mt5.copy_rates_range(symbol, TIMEFRAME_MAP[timeframe], date_from, date_to)
        if rates is None:
            code, msg = mt5.last_error()
            self.logger.error("Failed to fetch rates for %s: [%s] %s", symbol, code, msg)
            return None
        return rates

    # --- Broker rules helpers ---

    def get_broker_rules(self, symbol: str) -> Optional[BrokerRules]:
        info = self.ensure_symbol(symbol)
        if info is None:
            return None
        return BrokerRules(
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            stop_level_points=info.trade_stops_level,
            point=info.point,
            digits=getattr(info, "digits", 0) or 0,
            tick_size=float(getattr(info, "trade_tick_size", 0.0) or 0.0),
            tick_value=float(getattr(info, "trade_tick_value", 0.0) or 0.0),
            freeze_level_points=int(getattr(info, "trade_freeze_level", 0) or 0),
        )

    def normalize_volume(self, symbol: str, requested_volume: float) -> Optional[float]:
        rules = self.get_broker_rules(symbol)
        if rules is None:
            return None
        clipped = max(rules.volume_min, min(requested_volume, rules.volume_max))
        # align to step
        steps = math.floor((clipped - rules.volume_min) / rules.volume_step + 1e-9)
        normalized = rules.volume_min + steps * rules.volume_step
        normalized = round(normalized, 2)
        if normalized != requested_volume:
            self.logger.info(
                "Adjusted volume for %s from %.2f to %.2f (min=%.2f max=%.2f step=%.2f)",
                symbol,
                requested_volume,
                normalized,
                rules.volume_min,
                rules.volume_max,
                rules.volume_step,
            )
        return normalized

    def validate_stops(
        self,
        symbol: str,
        price: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        direction: str = "",
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate stop distances against broker stop level (in points).
        """
        rules = self.get_broker_rules(symbol)
        if rules is None:
            return False, "Missing broker rules"
        min_distance = max(rules.stop_level_points, rules.freeze_level_points) * rules.point
        side = str(direction or "").lower()

        if side == "buy":
            if stop_loss is not None and float(stop_loss) >= float(price) - min_distance:
                return False, f"Buy stop loss must be below price by >= {min_distance:.5f}"
            if take_profit is not None and float(take_profit) <= float(price) + min_distance:
                return False, f"Buy take profit must be above price by >= {min_distance:.5f}"
            return True, None

        if side == "sell":
            if stop_loss is not None and float(stop_loss) <= float(price) + min_distance:
                return False, f"Sell stop loss must be above price by >= {min_distance:.5f}"
            if take_profit is not None and float(take_profit) >= float(price) - min_distance:
                return False, f"Sell take profit must be below price by >= {min_distance:.5f}"
            return True, None

        if stop_loss is not None and abs(price - stop_loss) < min_distance:
            return False, f"Stop loss too close. Needs >= {min_distance:.5f}"
        if take_profit is not None and abs(price - take_profit) < min_distance:
            return False, f"Take profit too close. Needs >= {min_distance:.5f}"
        return True, None

    def send_market_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        deviation: int = 10,
        magic: int = 0,
        comment: str = "",
    ) -> Optional[mt5.TradeResult]:
        """
        Submit a broker-compliant market order. Returns TradeResult on success.
        """
        self.last_order_error = {}

        if not self.ensure_connected():
            self._set_last_order_error(symbol=symbol, stage="connection", message="MT5 is not connected")
            return None
        order_type = direction.lower()
        if order_type not in {"buy", "sell"}:
            raise ValueError("direction must be 'buy' or 'sell'")
        if not self.is_trading_allowed():
            msg = "Trading not allowed on this account. Aborting order send."
            self.logger.error(msg)
            self._set_last_order_error(symbol=symbol, stage="account", message=msg)
            return None

        info = self.ensure_symbol(symbol)
        if info is None:
            self._set_last_order_error(symbol=symbol, stage="symbol", message=f"{symbol} is not available")
            return None

        tradable, market_reason = self.is_symbol_tradable_now(symbol, order_type)
        if not tradable:
            msg = f"Order not sent: {market_reason}"
            self.logger.warning("%s | symbol=%s side=%s", msg, symbol, order_type.upper())
            self._set_last_order_error(symbol=symbol, stage="market", message=msg)
            return None

        normalized_volume = self.normalize_volume(symbol, volume)
        if normalized_volume is None:
            self._set_last_order_error(symbol=symbol, stage="volume", message=f"Invalid volume requested: {volume}")
            return None

        price = self._current_order_price(symbol, order_type, info)
        if price is None:
            msg = f"No tick data for {symbol}; cannot determine execution price"
            self.logger.error(msg)
            self._set_last_order_error(symbol=symbol, stage="price", message=msg)
            return None

        sl, tp = self._normalize_order_stops(symbol, order_type, stop_loss, take_profit, info)

        ok, err = self.validate_stops(symbol, price, sl or None, tp or None, direction=order_type)
        if not ok:
            msg = f"Stop validation failed for {symbol}: {err}"
            self.logger.error(msg)
            self._set_last_order_error(symbol=symbol, stage="stops", message=msg)
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": normalized_volume,
            "type": mt5.ORDER_TYPE_BUY if order_type == "buy" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": deviation,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._select_filling_mode(info),
        }

        self.logger.critical(
            "═══════════════════════════════════════════════════════════════════════════════════════\n"
            "🟢 SEND_MARKET_ORDER 🟢\n"
            "   Symbol: %s | Type: %s | Volume: %.2f | Price: %.5f\n"
            "   SL: %.5f | TP: %.5f | Magic: %d | Deviation: %d | Filling: %d\n"
            "   Comment: %s\n"
            "═══════════════════════════════════════════════════════════════════════════════════════",
            symbol, order_type.upper(), normalized_volume, price,
            sl, tp, magic, deviation, request["type_filling"], comment
        )

        invalid_fill_retcode = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", None)
        tried_modes = []
        for mode_attempt, mode in enumerate([
            request["type_filling"],
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]):
            if mode in tried_modes:
                continue
            tried_modes.append(mode)
            request["type_filling"] = mode

            fresh_price = self._current_order_price(symbol, order_type, info)
            if fresh_price is None:
                msg = f"No fresh tick data for {symbol}; cannot submit order"
                self.logger.error(msg)
                self._set_last_order_error(symbol=symbol, stage="price", message=msg)
                return None
            request["price"] = fresh_price

            ok, err = self.validate_stops(symbol, fresh_price, sl or None, tp or None, direction=order_type)
            if not ok:
                msg = f"Stop validation failed at live price for {symbol}: {err}"
                self.logger.error(msg)
                self._set_last_order_error(symbol=symbol, stage="stops", message=msg)
                return None

            check_ok, check_msg, check_retcode = self._order_check_request(symbol, request)
            if not check_ok:
                self.logger.warning(
                    "MT5_ORDER_CHECK_FAILED: symbol=%s | retcode=%d | %s",
                    symbol,
                    check_retcode,
                    check_msg,
                )
                if invalid_fill_retcode is not None and check_retcode == invalid_fill_retcode:
                    continue
                self._set_last_order_error(
                    symbol=symbol,
                    stage="order_check",
                    message=check_msg,
                    retcode=check_retcode,
                )
                return None

            self.logger.info("[MT5_ORDER_SEND] Attempt %d: filling_mode=%d | symbol=%s | %s %.2f",
                           mode_attempt + 1, mode, symbol, order_type.upper(), normalized_volume)

            result = mt5.order_send(request)
            if result is None:
                code, msg = mt5.last_error()
                self.logger.error(
                    "❌ MT5_ORDER_SEND_NONE: symbol=%s | error_code=%d | error_msg=%s",
                    symbol, code, msg
                )
                self._set_last_order_error(
                    symbol=symbol,
                    stage="order_send",
                    message=f"MT5 order_send returned None: [{code}] {msg}",
                    code=code,
                )
                return None

            retcode = int(getattr(result, "retcode", -1) or -1)
            result_comment = str(getattr(result, "comment", "") or "")
            order_id = int(getattr(result, "order", 0) or 0)
            deal_id = int(getattr(result, "deal", 0) or 0)

            self.logger.info(
                "[MT5_RESULT] retcode=%d | order=%d | deal=%d | comment='%s'",
                retcode, order_id, deal_id, result_comment
            )

            if retcode in self._price_retry_retcodes():
                fresh_price = self._current_order_price(symbol, order_type, info)
                if fresh_price is None:
                    msg = f"Price retry failed for {symbol}: no fresh tick"
                    self.logger.error(msg)
                    self._set_last_order_error(symbol=symbol, stage="price", message=msg)
                    return None
                request["price"] = fresh_price
                ok, err = self.validate_stops(symbol, fresh_price, sl or None, tp or None, direction=order_type)
                if not ok:
                    msg = f"Price retry stop validation failed for {symbol}: {err}"
                    self.logger.error(msg)
                    self._set_last_order_error(symbol=symbol, stage="stops", message=msg)
                    return None
                check_ok, check_msg, check_retcode = self._order_check_request(symbol, request)
                if not check_ok:
                    self.logger.error("ORDER_PRICE_RETRY_CHECK_FAILED: %s | %s", symbol, check_msg)
                    self._set_last_order_error(
                        symbol=symbol,
                        stage="order_check",
                        message=check_msg,
                        retcode=check_retcode,
                    )
                    return None
                self.logger.warning(
                    "ORDER_PRICE_RETRY: symbol=%s | retcode=%d | comment=%s | retry_price=%.5f",
                    symbol,
                    retcode,
                    result_comment,
                    fresh_price,
                )
                result = mt5.order_send(request)
                if result is None:
                    code, msg = mt5.last_error()
                    self._set_last_order_error(
                        symbol=symbol,
                        stage="order_send",
                        message=f"MT5 order_send retry returned None: [{code}] {msg}",
                        code=code,
                    )
                    return None
                retcode = int(getattr(result, "retcode", -1) or -1)
                result_comment = str(getattr(result, "comment", "") or "")
                order_id = int(getattr(result, "order", 0) or 0)
                deal_id = int(getattr(result, "deal", 0) or 0)
                self.logger.info(
                    "[MT5_RESULT_RETRY] retcode=%d | order=%d | deal=%d | comment='%s'",
                    retcode,
                    order_id,
                    deal_id,
                    result_comment,
                )

            if retcode == mt5.TRADE_RETCODE_DONE:
                self.last_order_error = {}
                self.logger.critical(
                    "✅ ORDER_EXECUTED: %s %.2f %s at %.5f | ticket=%d | deal=%d",
                    order_type.upper(),
                    normalized_volume,
                    symbol,
                    float(getattr(result, "price", 0.0) or request.get("price", price)),
                    order_id,
                    deal_id,
                )

                if sl or tp:
                    time.sleep(0.2)
                    pos_ticket = getattr(result, "position", None)
                    pos = None
                    if pos_ticket:
                        pos = self.get_position(int(pos_ticket), symbol=symbol)
                    if pos is None:
                        pos = self.get_position(int(result.order), symbol=symbol)
                    if pos is None:
                        pos = self.find_recent_position(
                            symbol=symbol,
                            direction=order_type,
                            volume=float(normalized_volume),
                            magic=int(magic),
                            comment=str(comment),
                        )
                    if pos is None:
                        self.logger.warning("Could not verify SL/TP on opened position (ticket=%s)", result.order)
                        return result

                    current_sl = float(getattr(pos, "sl", 0.0) or 0.0)
                    current_tp = float(getattr(pos, "tp", 0.0) or 0.0)
                    needs_update = False
                    if sl and abs(current_sl - float(sl)) > (info.point * 0.5):
                        needs_update = True
                    if tp and abs(current_tp - float(tp)) > (info.point * 0.5):
                        needs_update = True

                    if needs_update:
                        pos_id = int(getattr(pos, "ticket", 0) or getattr(pos, "identifier", 0) or result.order)
                        self.logger.info(
                            "SL/TP not applied on execution; updating position SL/TP | ticket=%s | sl=%.5f tp=%.5f",
                            pos_id,
                            float(sl),
                            float(tp),
                        )
                        self.set_position_sltp(
                            ticket=pos_id,
                            sl=float(sl),
                            tp=float(tp),
                            comment="SLTP after deal",
                        )
                return result

            is_invalid_fill = invalid_fill_retcode is not None and result.retcode == invalid_fill_retcode
            if is_invalid_fill or ("Unsupported filling mode" in (result.comment or "")):
                self.logger.warning(
                    "❌ ORDER_REJECTED_FILL: symbol=%s | retcode=%d | comment=%s | Trying next mode...",
                    symbol,
                    retcode,
                    result_comment,
                )
                continue

            self.logger.error(
                "❌ ORDER_REJECTED: symbol=%s | retcode=%d | comment=%s | NOT_RETRYING (fatal)",
                symbol,
                retcode,
                result_comment,
            )
            self._set_last_order_error(
                symbol=symbol,
                stage="broker",
                message=self._format_order_rejection(retcode, result_comment),
                retcode=retcode,
                comment=result_comment,
            )
            return None

        self.logger.error(
            "❌ ORDER_REJECTED_ALL_MODES: symbol=%s | No supported filling mode found | Tried: %s",
            symbol,
            tried_modes,
        )
        self._set_last_order_error(
            symbol=symbol,
            stage="filling",
            message=f"No supported filling mode found. Tried: {tried_modes}",
        )
        return None

    # --- Utility ---

    def __enter__(self):
        if not self.initialize():
            raise RuntimeError("Failed to initialize MT5 connection")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()

    # --- Internal helpers ---

    def _try_attach_to_running_terminal(self) -> bool:
        """
        Attempt to attach to a running MT5 terminal that is already logged in.
        This is useful when login/password initialization fails but the user
        has the terminal open and authorized.
        """
        self.logger.info("Attempting to attach to an already-authorized MT5 terminal session...")

        mt5.shutdown()
        init_kwargs = {"timeout": self.initialize_timeout_ms}
        if self.credentials.terminal_path:
            init_kwargs["path"] = self.credentials.terminal_path

        if not mt5.initialize(**init_kwargs):
            code, msg = mt5.last_error()
            self.logger.error("Attach initialize failed: [%s] %s", code, msg)
            if code == -6:
                self._log_authorization_troubleshooting(code, msg)
            return False

        info = mt5.account_info()
        if info is None:
            self.logger.error("Attached to terminal, but account info is unavailable.")
            mt5.shutdown()
            return False

        if not self._wait_for_terminal_connected():
            self.logger.error("MT5 terminal is not connected (no green bars).")
            mt5.shutdown()
            return False

        if self.credentials.login and int(self.credentials.login) > 0 and int(info.login) != int(self.credentials.login):
            self.logger.error(
                "Terminal is logged into a different account (expected=%s, actual=%s).",
                self.credentials.login,
                info.login,
            )
            mt5.shutdown()
            return False

        if getattr(info, "server", "") and self.credentials.server:
            if str(info.server).strip().lower() != str(self.credentials.server).strip().lower():
                self.logger.error(
                    "Terminal is connected to a different server (expected=%s, actual=%s).",
                    self.credentials.server,
                    info.server,
                )
                mt5.shutdown()
                return False

        self._log_account_status(info)
        self.connected = True
        self.logger.info("Attached to authorized terminal session successfully.")
        return True

    def _log_account_status(self, info: mt5.AccountInfo) -> None:
        try:
            trade_mode = {
                mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
                mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
                mt5.ACCOUNT_TRADE_MODE_REAL: "LIVE",
            }.get(info.trade_mode, f"UNKNOWN({info.trade_mode})")
        except AttributeError:  # older MetaTrader5 builds may miss constants
            trade_mode = str(info.trade_mode)

        self.logger.info(
            "Account status | login=%s | name=%s | currency=%s | trade_mode=%s | leverage=%s",
            info.login,
            info.name,
            info.currency,
            trade_mode,
            getattr(info, "leverage", "n/a"),
        )

    @staticmethod
    def _round_to_tick(price: Optional[float], digits: int, tick_size: float, mode: str = "nearest") -> float:
        if price is None:
            return 0.0
        value = float(price)
        tick = float(tick_size or 0.0)
        if tick > 0:
            scaled = value / tick
            if mode == "down":
                value = math.floor(scaled + 1e-12) * tick
            elif mode == "up":
                value = math.ceil(scaled - 1e-12) * tick
            else:
                value = round(scaled) * tick
        return round(value, int(digits or 0))

    def _normalize_price_for_symbol(
        self,
        symbol: str,
        price: Optional[float],
        info: Optional[mt5.SymbolInfo] = None,
        mode: str = "nearest",
    ) -> float:
        if price is None:
            return 0.0
        info = info or self.ensure_symbol(symbol)
        if info is None:
            return float(price)
        digits = int(getattr(info, "digits", 0) or 0)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
        return self._round_to_tick(float(price), digits, tick_size, mode=mode)

    def _current_order_price(self, symbol: str, order_type: str, info: mt5.SymbolInfo) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        raw_price = tick.ask if order_type == "buy" else tick.bid
        raw_price = float(raw_price or 0.0)
        if raw_price <= 0:
            return None
        return self._normalize_price_for_symbol(symbol, raw_price, info=info)

    def _normalize_order_stops(
        self,
        symbol: str,
        order_type: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        info: mt5.SymbolInfo,
    ) -> Tuple[float, float]:
        if order_type == "buy":
            sl_mode = "down"
            tp_mode = "up"
        else:
            sl_mode = "up"
            tp_mode = "down"
        sl = self._normalize_price_for_symbol(symbol, stop_loss, info=info, mode=sl_mode) if stop_loss is not None else 0.0
        tp = self._normalize_price_for_symbol(symbol, take_profit, info=info, mode=tp_mode) if take_profit is not None else 0.0
        return sl, tp

    def _order_check_request(self, symbol: str, request: Dict[str, Any]) -> Tuple[bool, str, int]:
        try:
            check = mt5.order_check(request)
        except Exception as exc:
            return False, f"order_check exception for {symbol}: {exc}", -1

        if check is None:
            code, msg = mt5.last_error()
            return False, f"order_check returned None: [{code}] {msg}", int(code or -1)

        raw_retcode = getattr(check, "retcode", -1)
        retcode = int(raw_retcode) if raw_retcode is not None else -1
        comment = str(getattr(check, "comment", "") or "")
        ok_codes = {
            0,
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
            int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        }
        if retcode in ok_codes:
            return True, "", retcode
        return False, self._format_order_rejection(retcode, comment), retcode

    @staticmethod
    def _price_retry_retcodes() -> set[int]:
        names = (
            "TRADE_RETCODE_REQUOTE",
            "TRADE_RETCODE_INVALID_PRICE",
            "TRADE_RETCODE_PRICE_CHANGED",
            "TRADE_RETCODE_PRICE_OFF",
        )
        values = set()
        for name in names:
            code = getattr(mt5, name, None)
            if code is not None:
                values.add(int(code))
        return values

    def _set_last_order_error(self, symbol: str, stage: str, message: str, **extra: Any) -> None:
        self.last_order_error = {
            "symbol": symbol,
            "stage": stage,
            "message": message,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            **extra,
        }

    @staticmethod
    def _format_order_rejection(retcode: int, comment: str = "") -> str:
        labels = (
            ("TRADE_RETCODE_REQUOTE", "requote"),
            ("TRADE_RETCODE_REJECT", "request rejected"),
            ("TRADE_RETCODE_CANCEL", "request cancelled"),
            ("TRADE_RETCODE_INVALID", "invalid request"),
            ("TRADE_RETCODE_INVALID_VOLUME", "invalid volume"),
            ("TRADE_RETCODE_INVALID_PRICE", "invalid price"),
            ("TRADE_RETCODE_INVALID_STOPS", "invalid SL/TP"),
            ("TRADE_RETCODE_TRADE_DISABLED", "trading disabled"),
            ("TRADE_RETCODE_MARKET_CLOSED", "market closed"),
            ("TRADE_RETCODE_NO_MONEY", "not enough margin"),
            ("TRADE_RETCODE_PRICE_CHANGED", "price changed"),
            ("TRADE_RETCODE_PRICE_OFF", "off quotes"),
            ("TRADE_RETCODE_INVALID_FILL", "invalid filling mode"),
            ("TRADE_RETCODE_CONNECTION", "connection problem"),
            ("TRADE_RETCODE_ONLY_REAL", "only allowed on real accounts"),
        )
        names = {}
        for attr, label in labels:
            code = getattr(mt5, attr, None)
            if code is not None:
                names[int(code)] = label
        reason = names.get(int(retcode), "broker rejected order")
        comment = str(comment or "").strip()
        if comment:
            return f"{reason} (retcode={retcode}, comment={comment})"
        return f"{reason} (retcode={retcode})"

    @staticmethod
    def _select_filling_mode(info: mt5.SymbolInfo) -> int:
        """
        Map broker SYMBOL_FILLING_MODE bitflags to MT5 trade request type_filling enum.
        SYMBOL_FILLING_FOK = 1 (bit 0) -> ORDER_FILLING_FOK = 0
        SYMBOL_FILLING_IOC = 2 (bit 1) -> ORDER_FILLING_IOC = 1
        """
        filling_mode_mask = getattr(info, "filling_mode", 0)
        if filling_mode_mask & 1:
            return getattr(mt5, "ORDER_FILLING_FOK", 0)
        if filling_mode_mask & 2:
            return getattr(mt5, "ORDER_FILLING_IOC", 1)
        return getattr(mt5, "ORDER_FILLING_RETURN", 2)

    def _wait_for_terminal_connected(self, wait_seconds: float = 10.0, poll_seconds: float = 0.5) -> bool:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            terminal = mt5.terminal_info()
            if terminal is not None and getattr(terminal, "connected", None) is True:
                return True
            time.sleep(poll_seconds)
        terminal = mt5.terminal_info()
        return bool(terminal is not None and getattr(terminal, "connected", None) is True)
