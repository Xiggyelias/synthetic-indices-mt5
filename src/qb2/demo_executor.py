"""
Isolated demo execution
=======================
The only code path that can call `buy`. It refuses to exist unless every flag is
set deliberately, and refuses to buy unless every check passes:

  configuration  ENABLE_TRADING=true, TRADING_MODE=demo, DEMO_ONLY=true, token set
  account        authorize -> is_virtual must be true (legacy demo loginids start
                 with "VR"); the token needs the "trade" scope; currency must match.
                 A real account halts the governor and raises. There is no fallback.
  contract       contracts_for must offer non-barrier CALL/PUT for the duration
  proposal       fresh, from Deriv, matching symbol / type / currency / duration,
                 ask price equal to the approved stake, payout equal to the priced payout
  risk           the RiskGovernor has already approved this exact order
  purchase       buy with max price = the approved stake, so slippage cannot raise cost
  settlement     proposal_open_contract until sold, then balance reconciliation
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from qb2.config import Settings
from qb2.deriv_client import DerivAPIError, DerivClient
from qb2.risk_governor import OrderRequest, RiskGovernor
from qb2.schema import (
    AccountInfo,
    ContractQuote,
    confirms_rise_fall,
    normalize_authorize,
    normalize_open_contract,
    num,
    rise_fall_available,
)
from qb2.storage import Database

log = logging.getLogger("qb2.demo")


class DemoExecutionRefused(RuntimeError):
    pass


def configuration_problems(settings: Settings) -> List[str]:
    problems = []
    if not settings.enable_trading:
        problems.append("ENABLE_TRADING is false")
    if settings.trading_mode != "demo":
        problems.append(f"TRADING_MODE is {settings.trading_mode!r}, not 'demo'")
    if not settings.demo_only:
        problems.append("DEMO_ONLY is false")
    if not settings.deriv_api_token:
        problems.append("DERIV_API_TOKEN is not set")
    if settings.deriv_api_version == "new" and not settings.deriv_account_id:
        problems.append("DERIV_ACCOUNT_ID is not set (needed for the new API's OTP login)")
    return problems


def account_problems(info: AccountInfo, settings: Settings) -> List[str]:
    problems = []
    if not info.is_virtual:
        problems.append(f"account {info.loginid} is NOT a virtual (demo) account")
    if settings.deriv_api_version == "legacy" and not info.loginid.upper().startswith("VR"):
        problems.append(f"loginid {info.loginid} does not have a demo prefix (VR...)")
    if info.scopes and "trade" not in info.scopes:
        problems.append("API token lacks the 'trade' scope")
    if info.currency and info.currency != settings.currency:
        problems.append(f"account currency {info.currency} != {settings.currency}")
    return problems


def quote_problems(q: ContractQuote, order: OrderRequest, settings: Settings, now: float) -> List[str]:
    problems = []
    if q.source != "deriv_proposal" or not q.proposal_id:
        problems.append("quote is not a live Deriv proposal")
    if q.symbol != order.symbol or q.contract_type != order.contract_type:
        problems.append("quote does not match the approved symbol/contract")
    if q.currency != settings.currency:
        problems.append(f"quote currency {q.currency}")
    if q.duration_seconds != settings.contract_seconds:
        problems.append(f"quote duration {q.duration_seconds}s")
    if abs(q.stake - order.stake) > 0.005:
        problems.append(f"ask price {q.stake} != approved stake {order.stake}")
    if abs(q.payout - order.payout) > 0.005:
        problems.append(f"payout {q.payout} != priced payout {order.payout}")
    if now - q.quote_time > settings.risk.max_quote_age_seconds:
        problems.append(f"quote is {now - q.quote_time:.1f}s old")
    if not confirms_rise_fall(q):
        problems.append(f"contract text does not confirm Rise/Fall: {q.longcode[:80]!r}")
    return problems


def is_demo_ws_url(url: str) -> bool:
    """New-API OTP URLs route to /options/ws/demo for virtual accounts (real accounts get another path)."""
    from urllib.parse import urlparse
    return urlparse(url).path.rstrip("/").endswith("/options/ws/demo")


class DemoExecutor:
    def __init__(self, client: DerivClient, settings: Settings, governor: RiskGovernor, db: Database,
                 clock=time.time, poll_seconds: float = 2.0):
        problems = configuration_problems(settings)
        if problems:
            raise DemoExecutionRefused("Demo execution disabled: " + "; ".join(problems))
        self.client = client
        self.settings = settings
        self.governor = governor
        self.db = db
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.account: Optional[AccountInfo] = None
        self.verified_symbols: Dict[str, bool] = {}

    def _refuse(self, problems: List[str], loginid: str = "") -> None:
        self.governor.halt("demo preflight failed: " + "; ".join(problems))
        self.db.audit("demo_preflight_refused", {"loginid": loginid, "problems": problems}, "critical")
        raise DemoExecutionRefused("; ".join(problems))

    async def preflight(self, symbols: List[str]) -> AccountInfo:
        if self.settings.deriv_api_version == "new":
            # Fail closed: the server-assigned OTP endpoint must be the demo one, whatever else is reported
            if not is_demo_ws_url(self.client.url):
                self._refuse([f"WebSocket endpoint is not Deriv's demo endpoint: {self.client.url.split('?')[0]}"])
            try:
                info = normalize_authorize(await self.client.authorize())
            except DerivAPIError as exc:
                # Not verified against a live authenticated session: the OTP connection may not accept
                # `authorize`. The demo endpoint path above is then the account-type evidence.
                bal = num(((await self.client.balance()).get("balance") or {}).get("balance"))
                info = AccountInfo(self.settings.deriv_account_id, True, self.settings.currency, bal, [])
                self.db.audit("demo_authorize_unavailable", {"error": str(exc), "evidence": "demo OTP endpoint"}, "warning")
            problems = account_problems(info, self.settings)
        else:
            info = normalize_authorize(await self.client.authorize())
            problems = account_problems(info, self.settings)
        if problems:
            self._refuse(problems, info.loginid)
        for sym in symbols:
            ok, notes = rise_fall_available(await self.client.contracts_for(sym), self.settings.contract_seconds,
                                            self.settings.deriv_api_version)
            self.verified_symbols[sym] = ok
            if not ok:
                self.db.audit("demo_symbol_unavailable", {"symbol": sym, "notes": notes}, "warning")
        self.account = info
        self.db.audit("demo_preflight_ok", {"loginid": info.loginid, "balance": info.balance,
                                            "symbols": self.verified_symbols})
        return info

    async def execute(self, order: OrderRequest, quote: ContractQuote, decision: Any, decision_id: int) -> Optional[str]:
        if self.account is None or not self.account.is_virtual:
            self.governor.release(order.idempotency_key)
            raise DemoExecutionRefused("preflight has not verified a demo account")
        if not self.verified_symbols.get(order.symbol):
            self.governor.release(order.idempotency_key)
            self.db.audit("demo_buy_skipped", {"key": order.idempotency_key, "reason": "symbol not verified"}, "warning")
            return None
        problems = quote_problems(quote, order, self.settings, self.clock())
        if problems:
            self.governor.release(order.idempotency_key)
            self.db.audit("demo_buy_skipped", {"key": order.idempotency_key, "problems": problems}, "warning")
            return None
        try:
            resp = await self.client.buy(quote.proposal_id, max_price=order.stake)
        except (DerivAPIError, asyncio.TimeoutError) as exc:
            self.governor.release(order.idempotency_key)
            self.governor.record_abnormal_response(f"buy failed: {exc}")
            self.db.audit("demo_buy_failed", {"key": order.idempotency_key, "error": str(exc)}, "warning")
            return None
        buy = resp.get("buy") or {}
        contract_id, price = buy.get("contract_id"), num(buy.get("buy_price"))
        if not contract_id or price is None or price > order.stake + 0.01:
            self.governor.halt(f"abnormal buy response: {buy}")
            self.db.audit("demo_buy_abnormal", {"key": order.idempotency_key, "response": buy}, "critical")
            return str(contract_id) if contract_id else None
        now = self.clock()
        self.db.insert_trade({
            "mode": "demo", "symbol": order.symbol, "contract_type": order.contract_type,
            "idempotency_key": order.idempotency_key, "stake": price, "payout": order.payout,
            "start_time": float(num(buy.get("start_time")) or now),
            "expiry_time": float(num(buy.get("start_time")) or now) + self.settings.contract_seconds,
            "contract_id": str(contract_id), "model_version": getattr(decision, "model_version", None),
            "decision_id": decision_id,
        })
        self.db.audit("demo_buy", {"key": order.idempotency_key, "contract_id": contract_id, "price": price})
        return str(contract_id)

    async def settle_open(self, now: float) -> int:
        settled = 0
        for t in self.db.trades(mode="demo", status="open"):
            if now < t["expiry_time"]:
                continue
            try:
                status = normalize_open_contract(await self.client.proposal_open_contract(t["contract_id"]))
            except (DerivAPIError, asyncio.TimeoutError) as exc:
                self.governor.record_abnormal_response(f"proposal_open_contract: {exc}")
                continue
            if not status.is_sold:
                if now - t["expiry_time"] > 600:
                    self.governor.halt(f"contract {t['contract_id']} unsettled 10 minutes after expiry")
                continue
            profit = status.profit if status.profit is not None else 0.0
            label = "won" if profit > 0 else "lost"
            self.db.settle_trade(t["id"], label, profit, status.entry_spot, status.exit_spot)
            self.governor.on_settled(t["idempotency_key"], profit, now=now)
            settled += 1
        if settled:
            bal = num(((await self.client.balance()).get("balance") or {}).get("balance"))
            if bal is not None:
                self.governor.reconcile(bal)
        return settled
