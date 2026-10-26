"""
Live Deriv Web Market Data Feed
===============================
Direct WebSocket client to Deriv's public API (wss://api.derivws.com/trading/v1/options/ws/public).
Provides real-time candles and tick data for synthetic volatility indices on Linux, Cloud, and Web.
Zero dependencies on Windows desktop MetaTrader 5 terminal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import websockets

logger = logging.getLogger("deriv-web-feed")

# Deriv official symbol mapping for synthetic indices
DERIV_SYMBOL_MAP: Dict[str, str] = {
    "Volatility 75 Index": "R_75",
    "Volatility 75 (1s) Index": "1HZ75V",
    "Volatility 100 Index": "R_100",
    "Volatility 100 (1s) Index": "1HZ100V",
    "Volatility 50 Index": "R_50",
    "Volatility 50 (1s) Index": "1HZ50V",
    "Volatility 25 Index": "R_25",
    "Volatility 25 (1s) Index": "1HZ25V",
    "Volatility 10 Index": "R_10",
    "Volatility 10 (1s) Index": "1HZ10V",
    "Crash 1000 Index": "CRASH1000",
    "Crash 500 Index": "CRASH500",
    "Crash 300 Index": "CRASH300N",
    "Boom 1000 Index": "BOOM1000",
    "Boom 500 Index": "BOOM500",
    "Boom 300 Index": "BOOM300N",
    "Step Index": "stpRNG",
}

# Legacy code mapping aliases
DERIV_CODE_ALIASES: Dict[str, str] = {
    "CRASH300": "CRASH300N",
    "BOOM300": "BOOM300N",
    "CRASH_1000": "CRASH1000",
    "CRASH_500": "CRASH500",
    "CRASH_300": "CRASH300N",
    "BOOM_1000": "BOOM1000",
    "BOOM_500": "BOOM500",
    "BOOM_300": "BOOM300N",
}


# Reverse lookup: code -> canonical human name
CODE_TO_NAME: Dict[str, str] = {v: k for k, v in DERIV_SYMBOL_MAP.items()}

# Fast lowercase lookup dictionary
LOWER_SYMBOL_MAP: Dict[str, str] = {
    k.lower(): v for k, v in DERIV_SYMBOL_MAP.items()
}
for code in DERIV_SYMBOL_MAP.values():
    LOWER_SYMBOL_MAP[code.lower()] = code

TIMEFRAME_SECONDS: Dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"


def normalize_deriv_symbol(symbol: str) -> str:
    """Resolve any symbol representation (human name, code, mixed case) to official Deriv code."""
    if not symbol:
        return "R_75"
    s = symbol.strip()
    s_upper = s.upper()
    if s_upper in DERIV_CODE_ALIASES:
        return DERIV_CODE_ALIASES[s_upper]
    if s in DERIV_SYMBOL_MAP.values():
        return s
    if s in DERIV_SYMBOL_MAP:
        return DERIV_SYMBOL_MAP[s]
    s_lower = s.lower()
    if s_lower in LOWER_SYMBOL_MAP:
        return LOWER_SYMBOL_MAP[s_lower]
    # Check without spaces or parentheses
    clean = s_lower.replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
    for k, v in DERIV_SYMBOL_MAP.items():
        k_clean = k.lower().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
        if clean == k_clean:
            return v
    return s


async def fetch_deriv_candles_async(
    symbol_name: str,
    timeframe: str = "M5",
    count: int = 150,
    timeout: float = 6.0,
) -> Optional[pd.DataFrame]:
    """Fetch live closed candles directly from Deriv public WebSocket API."""
    deriv_code = normalize_deriv_symbol(symbol_name)
    granularity = TIMEFRAME_SECONDS.get(timeframe.upper(), 300)

    try:
        async with websockets.connect(PUBLIC_WS_URL, open_timeout=timeout) as ws:
            req = {
                "ticks_history": deriv_code,
                "end": "latest",
                "count": min(count, 500),
                "granularity": granularity,
                "style": "candles",
            }
            await asyncio.wait_for(ws.send(json.dumps(req)), timeout=timeout)
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)

            candles = data.get("candles", [])
            if not candles:
                return None

            df = pd.DataFrame(candles)
            # Standardize column names to open, high, low, close, epoch
            df["time"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
            df["open"] = df["open"].astype(float)
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)
            df["close"] = df["close"].astype(float)
            df["tick_volume"] = 100
            return df
    except Exception as exc:
        logger.debug("Deriv live candle fetch for %s failed: %s", symbol_name, exc)
        return None


def get_deriv_live_candles(
    symbol_name: str,
    timeframe: str = "M5",
    count: int = 150,
) -> Optional[pd.DataFrame]:
    """Synchronous caller for Streamlit / sync routines."""
    try:
        loop = None
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            pass

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(lambda: asyncio.run(fetch_deriv_candles_async(symbol_name, timeframe, count)))
                return fut.result(timeout=7.0)
        else:
            return asyncio.run(fetch_deriv_candles_async(symbol_name, timeframe, count))
    except Exception as exc:
        logger.debug("Sync Deriv candle fetch error: %s", exc)
        return None


def test_deriv_web_connection() -> bool:
    """Test if Deriv public WebSocket endpoint is reachable."""
    try:
        df = get_deriv_live_candles("Volatility 75 Index", "M5", 2)
        return df is not None and len(df) > 0
    except Exception:
        return False


@dataclass
class DerivWebAccountInfo:
    """Account metadata representing the Deriv Cloud live market connection."""
    login: str = "Deriv-Public-Feed"
    server: str = "api.derivws.com (WebSocket)"
    equity: float = 10000.00
    balance: float = 10000.00
    currency: str = "USD"
    margin: float = 0.0
    margin_free: float = 10000.00
    leverage: int = 100


_DERIV_ACCOUNT_CACHE: Dict[str, Any] = {"time": 0.0, "data": None}


def fetch_deriv_cloud_accounts() -> Optional[List[Dict[str, Any]]]:
    """Fetch live Deriv account details using official options/accounts REST endpoint."""
    import os
    import time
    import urllib.request

    now = time.time()
    if now - _DERIV_ACCOUNT_CACHE["time"] < 15.0 and _DERIV_ACCOUNT_CACHE["data"]:
        return _DERIV_ACCOUNT_CACHE["data"]

    token = os.getenv("DERIV_API_TOKEN", "")
    app_id = os.getenv("DERIV_APP_ID", "")
    if not token or token == "YOUR_DERIV_API_TOKEN":
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if app_id:
        headers["Deriv-App-ID"] = app_id

    try:
        req = urllib.request.Request(
            "https://api.derivws.com/trading/v1/options/accounts",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            accs = body.get("data", [])
            if accs:
                _DERIV_ACCOUNT_CACHE["time"] = now
                _DERIV_ACCOUNT_CACHE["data"] = accs
                return accs
    except Exception as exc:
        logger.debug("Deriv account fetch error: %s", exc)

    return _DERIV_ACCOUNT_CACHE["data"]


class DerivWebConnection:
    """
    Drop-in web replacement for MT5Connection on Linux/Dokploy/Cloud environments.
    Connects directly to Deriv's live public WebSocket API for streaming market data.
    """

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or Path.cwd()
        self.server = "Deriv-WebSocket-Live"
        self._is_connected = True

    def initialize(self) -> bool:
        self._is_connected = test_deriv_web_connection()
        return self._is_connected

    def shutdown(self) -> None:
        self._is_connected = False

    def ensure_connected(self) -> bool:
        return True

    def ensure_symbol(self, symbol: str) -> Optional[str]:
        return normalize_deriv_symbol(symbol)

    def get_rates(
        self,
        symbol: str,
        timeframe: str = "M5",
        count: int = 150,
    ) -> Optional[pd.DataFrame]:
        return get_deriv_live_candles(symbol, timeframe, count)

    def get_symbol_price(self, symbol: str) -> Optional[Tuple[float, float]]:
        df = get_deriv_live_candles(symbol, "M1", 2)
        if df is not None and len(df) > 0:
            c = float(df["close"].iloc[-1])
            return (c, c)
        return None

    def get_account_info(self) -> DerivWebAccountInfo:
        import os
        eq = 10000.0
        bal = 10000.0
        state_file = self.root_dir / "data" / "synthetic_state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                eq = float(data.get("equity", 10000.0))
                bal = float(data.get("balance", 10000.0))
            except Exception:
                pass

        accounts = fetch_deriv_cloud_accounts()
        login_id = "Deriv-Public-Feed"
        if accounts:
            # Prefer active real account if funded, else demo account
            real_acc = next((a for a in accounts if a.get("account_type") == "real" and float(a.get("balance", 0)) > 0), None)
            demo_acc = next((a for a in accounts if a.get("account_type") == "demo"), None)
            target = real_acc or demo_acc or accounts[0]
            acc_id = target.get("account_id", "")
            acc_type = target.get("account_type", "").upper()
            login_id = f"{acc_id} ({acc_type})"
            try:
                raw_bal = float(target.get("balance", eq))
                if not state_file.exists():
                    bal = eq = raw_bal
            except Exception:
                pass
        else:
            token = os.getenv("DERIV_API_TOKEN", "")
            if token and token != "YOUR_DERIV_API_TOKEN":
                masked = f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "Active"
                login_id = f"PAT #{masked}"

        return DerivWebAccountInfo(login=login_id, equity=eq, balance=bal, margin_free=eq)
