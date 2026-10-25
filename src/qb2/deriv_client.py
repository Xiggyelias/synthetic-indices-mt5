"""
Deriv WebSocket client
======================
Async request/response client with:
  - req_id matching, so concurrent requests and subscriptions never mix
  - application-level {"ping": 1} keepalive (Deriv closes idle sessions after ~2 minutes)
  - per-request timeouts and typed API errors
  - subscription streams delivered through queues
  - disconnect callbacks feeding the risk governor's circuit breaker

The transport is injectable (`connect` returns an object with async send/recv/close),
so tests run against a fake server and never touch the network.

`buy` exists only for the isolated demo executor, which checks the account type
before it is ever called. Nothing else in the codebase calls it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

ConnectFn = Callable[[str], Awaitable[Any]]


class DerivAPIError(RuntimeError):
    def __init__(self, code: str, message: str, msg_type: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.msg_type = msg_type


class DerivConnectionError(RuntimeError):
    pass


async def _default_connect(url: str) -> Any:
    import websockets  # imported lazily so research code does not need the package
    return await websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2 ** 24, open_timeout=15)


class DerivClient:
    def __init__(
        self,
        url: str,
        token: str = "",
        api_version: str = "legacy",
        connect: Optional[ConnectFn] = None,
        ping_interval: float = 30.0,
        request_timeout: float = 20.0,
        logger: Optional[logging.Logger] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
        on_abnormal: Optional[Callable[[str], None]] = None,
    ):
        self.url = url
        self._token = token
        self.api_version = api_version
        self._connect_fn = connect or _default_connect
        self.ping_interval = ping_interval
        self.request_timeout = request_timeout
        self.logger = logger or logging.getLogger("qb2.deriv")
        self.on_disconnect = on_disconnect
        self.on_abnormal = on_abnormal

        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._streams: Dict[int, asyncio.Queue] = {}
        self._reader: Optional[asyncio.Task] = None
        self._pinger: Optional[asyncio.Task] = None
        self.connected = False

    # ── Connection ───────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._ws = await self._connect_fn(self.url)
        self.connected = True
        self._reader = asyncio.create_task(self._read_loop())
        if self.ping_interval > 0:
            self._pinger = asyncio.create_task(self._ping_loop())

    async def connect_with_retry(self, attempts: int = 6, base_delay: float = 1.0) -> None:
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                await self.connect()
                return
            except Exception as exc:  # network errors vary by transport
                last = exc
                delay = min(60.0, base_delay * 2 ** attempt) * (0.5 + random.random() / 2)
                self.logger.warning("Connect attempt %d failed (%s); retrying in %.1fs", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
        raise DerivConnectionError(f"Could not connect after {attempts} attempts: {last}")

    async def close(self) -> None:
        self.connected = False
        for task in (self._pinger, self._reader):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._fail_all(DerivConnectionError("client closed"))

    async def __aenter__(self) -> "DerivClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _fail_all(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for q in self._streams.values():
            q.put_nowait(exc)
        self._streams.clear()

    async def _read_loop(self) -> None:
        reason = "connection closed"
        try:
            while True:
                raw = await self._ws.recv()
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    self._abnormal(f"non-JSON message: {str(raw)[:120]}")
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        self.connected = False
        self.logger.warning("Deriv connection lost: %s", reason)
        if self.on_disconnect:
            self.on_disconnect(reason)
        self._fail_all(DerivConnectionError(reason))

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("req_id")
        if req_id is None:
            if msg.get("msg_type") != "ping":
                self._abnormal(f"message without req_id: {msg.get('msg_type')}")
            return
        req_id = int(req_id)
        if req_id in self._streams:
            self._streams[req_id].put_nowait(msg)
            fut = self._pending.pop(req_id, None)  # first stream message also answers the request
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def _abnormal(self, detail: str) -> None:
        self.logger.warning("Abnormal Deriv response: %s", detail)
        if self.on_abnormal:
            self.on_abnormal(detail)

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                try:
                    await self.send({"ping": 1})
                except Exception as exc:
                    self.logger.debug("ping failed: %s", exc)
        except asyncio.CancelledError:
            return

    # ── Requests ─────────────────────────────────────────────────────────────

    async def send(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send one request and return its response; raises DerivAPIError on an API error."""
        if not self.connected or self._ws is None:
            raise DerivConnectionError("not connected")
        req_id = next(self._ids)
        body = dict(payload, req_id=req_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps(body))
        try:
            msg = await asyncio.wait_for(fut, timeout or self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._abnormal(f"timeout waiting for {next(iter(payload))}")
            raise
        return self._check(msg)

    @staticmethod
    def _check(msg: Dict[str, Any]) -> Dict[str, Any]:
        err = msg.get("error")
        if err:
            raise DerivAPIError(str(err.get("code", "Error")), str(err.get("message", "")), str(msg.get("msg_type", "")))
        return msg

    async def subscribe(self, payload: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """Yield every message of a subscription until the connection drops."""
        if not self.connected or self._ws is None:
            raise DerivConnectionError("not connected")
        req_id = next(self._ids)
        queue: asyncio.Queue = asyncio.Queue()
        self._streams[req_id] = queue
        await self._ws.send(json.dumps(dict(payload, subscribe=1, req_id=req_id)))
        try:
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    raise item
                yield self._check(item)
        finally:
            self._streams.pop(req_id, None)

    # ── API calls ────────────────────────────────────────────────────────────

    async def active_symbols(self) -> Dict[str, Any]:
        return await self.send({"active_symbols": "brief"})

    async def contracts_for(self, symbol: str) -> Dict[str, Any]:
        resp = await self.send({"contracts_for": symbol})
        return resp.get("contracts_for") or {}

    async def ticks_history(self, symbol: str, end: Any = "latest", count: int = 5000,
                            start: Optional[int] = None) -> Dict[str, Any]:
        req: Dict[str, Any] = {"ticks_history": symbol, "end": end, "style": "ticks", "count": int(count)}
        if start is not None:
            req["start"] = int(start)
        return await self.send(req, timeout=60)

    async def proposal(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return await self.send(request)

    async def authorize(self) -> Dict[str, Any]:
        if not self._token:
            raise DerivAPIError("NoToken", "DERIV_API_TOKEN is not configured", "authorize")
        return await self.send({"authorize": self._token})

    async def balance(self) -> Dict[str, Any]:
        return await self.send({"balance": 1})

    async def buy(self, proposal_id: str, max_price: float) -> Dict[str, Any]:
        """Buy at most `max_price`. Called only by DemoExecutor after its account checks."""
        return await self.send({"buy": proposal_id, "price": round(float(max_price), 2)})

    async def proposal_open_contract(self, contract_id: Any) -> Dict[str, Any]:
        return await self.send({"proposal_open_contract": 1, "contract_id": int(contract_id)})

    async def profit_table(self, limit: int = 50, date_from: Optional[int] = None) -> Dict[str, Any]:
        req: Dict[str, Any] = {"profit_table": 1, "description": 1, "limit": int(limit), "sort": "DESC"}
        if date_from is not None:
            req["date_from"] = int(date_from)
        return await self.send(req)


def fetch_new_api_ws_url(account_id: str, token: str, app_id: str = "",
                         base_url: str = "https://api.derivws.com") -> str:
    """
    New-API authentication: POST /trading/v1/options/accounts/{id}/otp returns a
    one-time WebSocket URL (demo or real depending on the account id). The caller
    must still pass the account through DemoExecutor.preflight before trading.
    """
    import urllib.request

    req = urllib.request.Request(
        f"{base_url}/trading/v1/options/accounts/{account_id}/otp",
        method="POST",
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 **({"Deriv-App-ID": app_id} if app_id else {})},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    url = (body.get("data") or {}).get("url")
    if not url:
        raise DerivAPIError("OTPFailed", f"No WebSocket URL in OTP response: {list(body)}")
    return str(url)
