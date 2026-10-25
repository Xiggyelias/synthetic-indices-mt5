#!/usr/bin/env python3
"""
Quantum Brain v2: research and decision CLI for Deriv Volatility Indices
========================================================================
Research workflow (reproducible; every result is stored with its dataset hash):

  python quantum_brain.py migrate
  python quantum_brain.py discover                         # needs DERIV_APP_ID
  python quantum_brain.py backfill --symbols R_100,R_75 --hours 168
  python quantum_brain.py stream --symbols R_100,R_75 --quote-every 60
  python quantum_brain.py quality
  python quantum_brain.py research --symbols R_100,R_75
  python quantum_brain.py backtest --symbol R_100 [--payout-ratio 1.95]
  python quantum_brain.py train --symbol R_100             # creates a candidate only
  python quantum_brain.py models
  python quantum_brain.py promote --version V --approver "Your Name"
  python quantum_brain.py rollback --symbol R_100 --approver "Your Name"
  python quantum_brain.py drift --symbol R_100
  python quantum_brain.py paper --symbols R_100            # paper decisions on live quotes
  python quantum_brain.py demo --symbols R_100             # isolated demo execution
  python quantum_brain.py status
  python quantum_brain.py reset-halt --operator "Your Name" --note "reason"

Offline dry run without an API (data stored as "SIM:<symbol>", never mixed with real data):
  python quantum_brain.py simulate --symbol R_100 --days 5

Defaults: TRADING_MODE=paper, DEMO_ONLY=true, ENABLE_TRADING=false. There is no
real-money mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from qb2.config import ConfigError, load_settings  # noqa: E402
from qb2.registry import ModelRegistry  # noqa: E402
from qb2.risk_governor import RiskGovernor  # noqa: E402
from qb2.storage import Database  # noqa: E402

log = logging.getLogger("qb2")


def setup_logging(verbose: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(ROOT / "logs" / "quantum_brain.log", encoding="utf-8")])


def dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def symbols_arg(value: str, settings) -> list:
    syms = [s.strip() for s in (value or "").split(",") if s.strip()] or list(settings.symbols)
    if not syms:
        raise SystemExit("No symbols given (use --symbols or QB2_SYMBOLS)")
    return syms


def make_client(settings, governor=None):
    from qb2.deriv_client import DerivClient
    return DerivClient(
        settings.ws_url_with_app_id, token=settings.deriv_api_token, api_version=settings.deriv_api_version,
        on_disconnect=(lambda reason: governor.record_disconnect()) if governor else None,
        on_abnormal=(lambda detail: governor.record_abnormal_response(detail)) if governor else None,
    )


def make_governor(settings, db, mode: str, initial_equity: float):
    return RiskGovernor(settings.risk, initial_equity, store=db, state_key=f"risk_governor:{mode}",
                        audit=lambda e, p, s: db.audit(e, p, s))


async def cmd_discover(settings, db, args):
    from qb2.collector import discover_instruments
    async with make_client(settings) as client:
        insts = await discover_instruments(client, db, settings)
    dump([i.as_dict() for i in insts])


async def cmd_backfill(settings, db, args):
    from qb2.collector import backfill_ticks
    async with make_client(settings) as client:
        for sym in symbols_arg(args.symbols, settings):
            n = await backfill_ticks(client, db, sym, args.hours)
            print(f"{sym}: {n} new ticks")


async def cmd_stream(settings, db, args):
    from qb2.collector import fetch_quotes, stream_ticks
    syms = symbols_arg(args.symbols, settings)
    stop = asyncio.Event()
    governor = make_governor(settings, db, "paper", args.equity)

    async def quote_loop():
        while not stop.is_set():
            stake = governor.stake_for() or settings.risk.min_stake
            try:
                async with make_client(settings) as client:
                    while not stop.is_set():
                        for sym in syms:
                            await fetch_quotes(client, db, settings, sym, stake)
                        await asyncio.sleep(args.quote_every)
            except Exception as exc:
                log.warning("quote loop error: %s", exc)
                await asyncio.sleep(10)

    tasks = [asyncio.create_task(stream_ticks(lambda: make_client(settings), db, syms, stop,
                                              on_disconnect=governor.record_disconnect))]
    if args.quote_every > 0:
        tasks.append(asyncio.create_task(quote_loop()))
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()


def cmd_simulate(settings, db, args):
    from qb2.ticks import simulate_volatility_ticks
    sym = f"SIM:{args.symbol}"
    df = simulate_volatility_ticks(args.symbol, seconds=int(args.days * 86400), seed=args.seed, ou_ratio=args.ou_ratio)
    run = db.start_run("simulated", "simulator", sym, {"days": args.days, "seed": args.seed, "ou_ratio": args.ou_ratio})
    n = db.insert_ticks(sym, df["epoch"].values, df["quote"].values, run)
    db.finish_run(run, "ok", n, "SIMULATED DATA: not evidence about the real market")
    print(f"Inserted {n} simulated ticks as {sym}")


def cmd_quality(settings, db, args):
    from qb2.data_quality import clean_ticks
    syms = [s["symbol"] for s in db.tick_summary()] if not args.symbols else symbols_arg(args.symbols, settings)
    out = []
    for sym in syms:
        _, rep = clean_ticks(db.load_ticks(sym), sym)
        db.save_quality_report(sym, rep.passed, rep.as_dict())
        out.append(rep.as_dict())
    dump(out)


def cmd_research(settings, db, args):
    from qb2.pipeline import research_symbols
    syms = symbols_arg(args.symbols, settings) if args.symbols or settings.symbols else [s["symbol"] for s in db.tick_summary()]
    dump(research_symbols(db, settings, syms)["ranking"])


def cmd_backtest(settings, db, args):
    from qb2.pipeline import backtest_symbol
    res = backtest_symbol(db, settings, args.symbol, args.model, args.payout_ratio)
    res.pop("equity_curve", None)
    dump(res)


def cmd_train(settings, db, args):
    from qb2.pipeline import train_symbol
    dump(train_symbol(db, settings, args.symbol, ModelRegistry(db, settings.model_dir),
                      allow_holdout_reuse=args.allow_holdout_reuse))


def cmd_models(settings, db, args):
    dump([{k: m[k] for k in ("version", "symbol", "model_type", "status", "approved_by", "notes")}
          | {"acceptance": m["metrics"].get("acceptance")} for m in db.models(symbol=args.symbol)])


def cmd_promote(settings, db, args):
    ModelRegistry(db, settings.model_dir).promote(args.version, args.approver, args.note or "")
    print(f"Promoted {args.version} (approved by {args.approver})")


def cmd_rollback(settings, db, args):
    v = ModelRegistry(db, settings.model_dir).rollback(args.symbol, args.approver, args.note or "")
    print(f"Rolled back {args.symbol} to {v}")


def cmd_drift(settings, db, args):
    from qb2.pipeline import drift_check
    dump(drift_check(db, settings, ModelRegistry(db, settings.model_dir), args.symbol))


async def cmd_paper(settings, db, args):
    from qb2.collector import fetch_quotes
    from qb2.trader import Trader
    syms = symbols_arg(args.symbols, settings)
    governor = make_governor(settings, db, "paper", args.equity)
    registry = ModelRegistry(db, settings.model_dir)
    if args.offline:
        trader = Trader(settings, db, registry, governor, quote_source=None)
        for sym in syms:
            dump(( await trader.step(sym)).as_dict())
        return
    async with make_client(settings, governor) as client:
        trader = Trader(settings, db, registry, governor,
                        quote_source=lambda sym, stake: fetch_quotes(client, db, settings, sym, stake))
        if args.once:
            for sym in syms:
                dump((await trader.step(sym)).as_dict())
            return
        stop = asyncio.Event()
        await trader.run(syms, stop)


async def cmd_demo(settings, db, args):
    from qb2.collector import fetch_quotes
    from qb2.demo_executor import DemoExecutionRefused, DemoExecutor, configuration_problems
    from qb2.trader import Trader
    problems = configuration_problems(settings)
    if problems:
        raise SystemExit("Demo execution refused: " + "; ".join(problems))
    syms = symbols_arg(args.symbols, settings)
    governor = make_governor(settings, db, "demo", 0.0)
    if settings.deriv_api_version == "new":
        from qb2.deriv_client import DerivClient, fetch_new_api_ws_url
        url = fetch_new_api_ws_url(settings.deriv_account_id, settings.deriv_api_token, settings.deriv_app_id)
        client = DerivClient(url, token=settings.deriv_api_token, api_version="new")
    else:
        client = make_client(settings)
    client.on_disconnect = lambda reason: governor.record_disconnect()
    client.on_abnormal = lambda detail: governor.record_abnormal_response(detail)
    async with client:
        executor = DemoExecutor(client, settings, governor, db)
        try:
            info = await executor.preflight(syms)    # refuses (and halts) unless the account is virtual
        except DemoExecutionRefused as exc:
            raise SystemExit(f"Demo preflight refused: {exc}")
        if info.balance is None:
            raise SystemExit("Demo preflight could not read the account balance")
        governor.initialize_equity(info.balance)
        governor.reconcile(info.balance)
        trader = Trader(settings, db, ModelRegistry(db, settings.model_dir), governor,
                        quote_source=lambda sym, stake: fetch_quotes(client, db, settings, sym, stake),
                        executor=executor)
        await trader.run(syms, asyncio.Event())


def cmd_status(settings, db, args):
    from qb2.pipeline import status
    dump(status(db, settings))


def cmd_reset_halt(settings, db, args):
    if args.symbol:
        from qb2.pipeline import clear_strategy_halt
        clear_strategy_halt(db, args.symbol, args.operator, args.note)
        print(f"Cleared strategy halt for {args.symbol}")
        return
    gov = make_governor(settings, db, args.mode, 0.0)
    gov.reset_halt(args.operator, args.note)
    print(f"Cleared {args.mode} risk halt")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Quantum Brain v2: statistical edge engine for Deriv Volatility Indices")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", default=None, help="JSON config (e.g. config/config_quantum.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="create or upgrade the research database")
    sub.add_parser("discover", help="discover Volatility Indices and Rise/Fall availability")
    b = sub.add_parser("backfill", help="download historical ticks")
    b.add_argument("--symbols", default="")
    b.add_argument("--hours", type=float, default=72)
    s = sub.add_parser("stream", help="collect live ticks and proposals")
    s.add_argument("--symbols", default="")
    s.add_argument("--quote-every", type=float, default=60, help="seconds between proposal snapshots (0 = off)")
    s.add_argument("--equity", type=float, default=1000.0, help="paper equity used to size quote stakes")
    sim = sub.add_parser("simulate", help="insert SIMULATED ticks for an offline dry run")
    sim.add_argument("--symbol", default="R_100")
    sim.add_argument("--days", type=float, default=5)
    sim.add_argument("--seed", type=int, default=0)
    sim.add_argument("--ou-ratio", type=float, default=0.0, help="plant an artificial edge (tests only)")
    q = sub.add_parser("quality", help="data quality report")
    q.add_argument("--symbols", default="")
    r = sub.add_parser("research", help="research and rank instruments")
    r.add_argument("--symbols", default="")
    bt = sub.add_parser("backtest", help="contract-aware walk-forward backtest")
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--model", default="logistic", choices=["base_rate", "logistic", "gbm", "ensemble"])
    bt.add_argument("--payout-ratio", type=float, default=None,
                    help="ASSUMED payout/stake when no proposals are recorded (marks the result INCOMPLETE)")
    t = sub.add_parser("train", help="train and register a candidate model (never promotes)")
    t.add_argument("--symbol", required=True)
    t.add_argument("--allow-holdout-reuse", action="store_true")
    m = sub.add_parser("models", help="list registered models")
    m.add_argument("--symbol", default=None)
    pr = sub.add_parser("promote", help="promote a validated model (human approval)")
    pr.add_argument("--version", required=True)
    pr.add_argument("--approver", required=True)
    pr.add_argument("--note", default="")
    rb = sub.add_parser("rollback", help="restore the previous champion")
    rb.add_argument("--symbol", required=True)
    rb.add_argument("--approver", required=True)
    rb.add_argument("--note", default="")
    d = sub.add_parser("drift", help="drift check for the champion model")
    d.add_argument("--symbol", required=True)
    pa = sub.add_parser("paper", help="paper decision loop on live quotes")
    pa.add_argument("--symbols", default="")
    pa.add_argument("--equity", type=float, default=1000.0)
    pa.add_argument("--once", action="store_true")
    pa.add_argument("--offline", action="store_true", help="one step per symbol without an API (always NO_TRADE)")
    de = sub.add_parser("demo", help="isolated demo-account execution (requires ENABLE_TRADING=true, TRADING_MODE=demo)")
    de.add_argument("--symbols", default="")
    sub.add_parser("status", help="system status")
    rh = sub.add_parser("reset-halt", help="clear a latched risk or strategy halt")
    rh.add_argument("--operator", required=True)
    rh.add_argument("--note", required=True)
    rh.add_argument("--mode", default="paper", choices=["paper", "demo"])
    rh.add_argument("--symbol", default=None, help="clear a per-symbol strategy (drift) halt instead")
    return p


COMMANDS = {
    "discover": cmd_discover, "backfill": cmd_backfill, "stream": cmd_stream, "simulate": cmd_simulate,
    "quality": cmd_quality, "research": cmd_research, "backtest": cmd_backtest, "train": cmd_train,
    "models": cmd_models, "promote": cmd_promote, "rollback": cmd_rollback, "drift": cmd_drift,
    "paper": cmd_paper, "demo": cmd_demo, "status": cmd_status, "reset-halt": cmd_reset_halt,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        settings = load_settings(config_path=args.config)
    except ConfigError as exc:
        print(f"Configuration refused: {exc}", file=sys.stderr)
        return 2
    db = Database(settings.db_path)
    applied = db.migrate()
    if args.cmd == "migrate":
        print(f"Applied: {applied or 'nothing (up to date)'}")
        return 0
    fn = COMMANDS[args.cmd]
    result = fn(settings, db, args)
    if asyncio.iscoroutine(result):
        try:
            asyncio.run(result)
        except KeyboardInterrupt:
            print("Stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
