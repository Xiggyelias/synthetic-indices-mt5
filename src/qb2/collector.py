"""
Deriv data collection
=====================
  discover_instruments  active_symbols -> Volatility Indices -> contracts_for check
                        that non-barrier CALL/PUT covers the configured duration
  backfill_ticks        paginated ticks_history going backwards from now
  stream_ticks          live tick subscriptions with reconnect and backoff; every
                        disconnect is reported to the risk governor
  fetch_quotes          Rise/Fall proposals (no purchase) for both sides; each one is
                        stored, building the historical payout record the backtester
                        needs
All ingestion is recorded in ingestion_runs for lineage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from qb2.config import Settings
from qb2.deriv_client import DerivAPIError, DerivClient, DerivConnectionError
from qb2.schema import (
    FALL,
    RISE,
    ContractQuote,
    Instrument,
    confirms_rise_fall,
    instruments_from_active_symbols,
    normalize_proposal,
    normalize_tick,
    normalize_ticks_history,
    proposal_request,
    rise_fall_available,
    volatility_instruments,
)
from qb2.storage import Database

log = logging.getLogger("qb2.collector")


async def discover_instruments(client: DerivClient, db: Database, settings: Settings,
                               probe_stake: float = 1.0) -> List[Instrument]:
    """
    Availability needs both: contracts_for covering the duration, and a barrier-free
    proposal for each side whose contract text matches the modelled settlement rule.
    The probe proposals are stored like any other quote (nothing is bought).
    """
    resp = await client.active_symbols()
    vols = volatility_instruments(instruments_from_active_symbols(resp))
    for inst in vols:
        notes: List[str]
        try:
            ok, notes = rise_fall_available(await client.contracts_for(inst.symbol), settings.contract_seconds,
                                            settings.deriv_api_version)
        except DerivAPIError as exc:
            ok, notes = None, [f"contracts_for failed: {exc}"]
        if ok is not False:
            quotes = await fetch_quotes(client, db, settings, inst.symbol, max(probe_stake, settings.risk.min_stake))
            confirmed = len(quotes) == 2 and all(confirms_rise_fall(q) for q in quotes.values())
            if confirmed:
                notes.append("payout/stake " + ", ".join(f"{k} {q.payout / q.stake:.3f}" for k, q in sorted(quotes.items())))
            else:
                notes.append("probe proposal did not confirm Rise/Fall: "
                             + "; ".join(f"{k}: {q.longcode[:80]}" for k, q in quotes.items()) if quotes else "probe proposal failed")
            ok = confirmed
        inst.rise_fall_5m = ok
        db.upsert_instrument(inst.as_dict(), "; ".join(notes))
    db.audit("instruments_discovered", {"count": len(vols), "symbols": [i.symbol for i in vols],
                                        "rise_fall_ok": [i.symbol for i in vols if i.rise_fall_5m]})
    return vols


async def backfill_ticks(client: DerivClient, db: Database, symbol: str, hours: float,
                         page: int = 5000, max_pages: int = 10000) -> int:
    run = db.start_run("backfill", "deriv_api", symbol, {"hours": hours, "page": page})
    target_start = int(time.time() - hours * 3600)
    end: Any = "latest"
    total = 0
    status, note = "ok", ""
    try:
        for _ in range(max_pages):
            e, q = normalize_ticks_history(await client.ticks_history(symbol, end=end, count=page))
            if len(e) == 0:
                break
            total += db.insert_ticks(symbol, e, q, run)
            next_end = int(e[0]) - 1
            # Page sizes are capped server-side (1000 on the new API), so a short page does not
            # mean history is exhausted; stop on the target or when a page makes no progress.
            if e[0] <= target_start or (end != "latest" and next_end >= end):
                break
            end = next_end
    except Exception as exc:
        status, note = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        db.finish_run(run, status, total, note)
    log.info("Backfilled %d ticks for %s", total, symbol)
    return total


async def stream_ticks(
    client_factory: Callable[[], DerivClient],
    db: Database,
    symbols: Iterable[str],
    stop: asyncio.Event,
    on_disconnect: Optional[Callable[[], None]] = None,
    on_tick: Optional[Callable[[str, int, float], Any]] = None,
    max_backoff: float = 60.0,
) -> None:
    symbols = list(symbols)
    attempt = 0
    while not stop.is_set():
        client = client_factory()
        run = db.start_run("stream", "deriv_api", ",".join(symbols), {})
        count = 0
        try:
            await client.connect_with_retry()
            attempt = 0

            async def one(sym: str) -> None:
                nonlocal count
                async for msg in client.subscribe({"ticks": sym}):
                    tick = normalize_tick(msg)
                    if tick is None:
                        continue
                    count += db.insert_ticks(tick[0], [tick[1]], [tick[2]], run)
                    if on_tick:
                        on_tick(*tick)
                    if stop.is_set():
                        return

            tasks = [asyncio.create_task(one(s)) for s in symbols]
            stopper = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(tasks + [stopper], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t is not stopper and t.exception():
                    raise t.exception()
            db.finish_run(run, "ok", count)
        except (DerivConnectionError, DerivAPIError, OSError, asyncio.TimeoutError) as exc:
            db.finish_run(run, "disconnected", count, str(exc))
            db.audit("stream_disconnected", {"error": str(exc)}, "warning")
            if on_disconnect:
                on_disconnect()
            attempt += 1
            await asyncio.sleep(min(max_backoff, 2 ** attempt))
        finally:
            await client.close()


async def fetch_quotes(client: DerivClient, db: Optional[Database], settings: Settings,
                       symbol: str, stake: float) -> Dict[str, ContractQuote]:
    """Price both sides without buying. Failed sides are omitted (the decision engine then refuses)."""
    out: Dict[str, ContractQuote] = {}
    for ctype in (RISE, FALL):
        req = proposal_request(symbol, ctype, stake, settings.duration, settings.duration_unit,
                               settings.currency, settings.deriv_api_version)
        try:
            resp = await client.proposal(req)
            q = normalize_proposal(resp, symbol, ctype, settings.contract_seconds, settings.currency, time.time())
        except (DerivAPIError, ValueError, asyncio.TimeoutError) as exc:
            log.warning("Proposal %s %s failed: %s", symbol, ctype, exc)
            if db is not None:
                db.audit("proposal_failed", {"symbol": symbol, "contract_type": ctype, "error": str(exc)}, "warning")
            continue
        out[ctype] = q
        if db is not None:
            db.record_quote(q.as_dict())
    return out
