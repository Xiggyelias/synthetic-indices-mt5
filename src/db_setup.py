#!/usr/bin/env python3
"""
MySQL Database Setup & Migration
=================================
Run this once after MySQL is installed to:
1. Create the trading_bot database and all tables
2. Migrate any existing JSON data into MySQL

Usage:
    python src/db_setup.py
"""

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
log = logging.getLogger("db_setup")

# Load .env
from pathlib import Path
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

sys.path.insert(0, str(Path(__file__).parent))
from database import DatabaseManager, MYSQL_AVAILABLE


def check_mysql() -> bool:
    if not MYSQL_AVAILABLE:
        log.error("mysql-connector-python is not installed.")
        log.error("Run: pip install mysql-connector-python")
        return False
    return True


def test_connection(db: DatabaseManager) -> bool:
    if not db.available:
        log.error("Cannot connect to MySQL. Check MYSQL_HOST/USER/PASSWORD in .env")
        log.error("  MYSQL_HOST=%s", os.getenv("MYSQL_HOST", "127.0.0.1"))
        log.error("  MYSQL_USER=%s", os.getenv("MYSQL_USER", "root"))
        log.error("  MYSQL_DATABASE=%s", os.getenv("MYSQL_DATABASE", "trading_bot"))
        return False
    log.info("MySQL connection OK")
    return True


def migrate_performance_state(db: DatabaseManager) -> int:
    """Migrate data/performance_state.json → trades table."""
    path = Path(__file__).parent.parent / "data" / "performance_state.json"
    if not path.exists():
        log.info("No performance_state.json found — skipping migration")
        return 0
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    count = 0
    for t in raw.get("trades", []):
        try:
            t["confluence_signals"] = t.get("confluence_signals") or {}
            t["win"] = bool(t.get("win", False))
            db.insert_trade(t)
            count += 1
        except Exception as e:
            log.warning("Skipping trade (ticket=%s): %s", t.get("ticket"), e)
    log.info("Migrated %d trades from performance_state.json", count)
    return count


def migrate_trade_journal(db: DatabaseManager) -> int:
    """Migrate data/trade_journal.json → journal_entries table."""
    path = Path(__file__).parent.parent / "data" / "trade_journal.json"
    if not path.exists():
        log.info("No trade_journal.json found — skipping migration")
        return 0
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    count = 0
    for e in raw.get("entries", []):
        try:
            e["confluence_signals"] = e.get("confluence_signals") or {}
            e["tags"] = e.get("tags") or []
            e["moved_to_be"] = int(bool(e.pop("moved_to_be", False)))
            e["partial_closed"] = int(bool(e.pop("partial_closed", False)))
            e["trailing_used"] = int(bool(e.pop("trailing_used", False)))
            e["structure_break_exit"] = int(bool(e.pop("structure_break_exit", False)))
            db.insert_journal_entry(e)
            count += 1
        except Exception as ex:
            log.warning("Skipping journal entry (ticket=%s): %s", e.get("ticket"), ex)
    log.info("Migrated %d journal entries", count)
    return count


def migrate_forward_test(db: DatabaseManager) -> int:
    """Migrate config/forward_test_config.json → forward_test_trades / daily_metrics."""
    path = Path(__file__).parent.parent / "config" / "forward_test_config.json"
    if not path.exists():
        log.info("No forward_test_config.json found — skipping migration")
        return 0
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    trade_count = 0
    for t in raw.get("trades", []):
        try:
            from datetime import datetime
            db.upsert_ft_trade({
                "trade_id": int(t["trade_id"]),
                "direction": t["direction"],
                "entry_time": datetime.fromisoformat(t["entry_time"]) if t.get("entry_time") else None,
                "entry_price": float(t["entry_price"]),
                "expected_entry_price": float(t.get("expected_entry_price", t["entry_price"])),
                "exit_time": datetime.fromisoformat(t["exit_time"]) if t.get("exit_time") else None,
                "exit_price": float(t["exit_price"]) if t.get("exit_price") is not None else None,
                "profit_loss": float(t["profit_loss"]) if t.get("profit_loss") is not None else None,
                "slippage_pips": float(t.get("slippage_pips", 0.0)),
                "spread_at_entry": float(t.get("spread_at_entry", 0.0)),
                "news_affected": bool(t.get("news_affected", False)),
                "exit_reason": t.get("exit_reason"),
            })
            trade_count += 1
        except Exception as ex:
            log.warning("Skipping FT trade: %s", ex)

    metric_count = 0
    for d in raw.get("daily_metrics", []):
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(d["date"])
            db.upsert_daily_metrics({
                "metric_date": dt.date(),
                "trades_taken": int(d["trades_taken"]),
                "total_pnl": float(d["total_pnl"]),
                "total_slippage": float(d["total_slippage"]),
                "max_spread": float(d["max_spread"]),
                "uptime_percentage": float(d["uptime_percentage"]),
                "news_blocks": int(d["news_blocks"]),
                "connection_issues": int(d["connection_issues"]),
            })
            metric_count += 1
        except Exception as ex:
            log.warning("Skipping daily metric: %s", ex)

    log.info("Migrated %d forward-test trades, %d daily metrics", trade_count, metric_count)
    return trade_count + metric_count


def main():
    print("=" * 50)
    print("  Trading Bot — MySQL Setup & Migration")
    print("=" * 50)

    if not check_mysql():
        sys.exit(1)

    db = DatabaseManager.from_env()
    if not test_connection(db):
        print()
        print("To fix: set MYSQL_PASSWORD in .env and ensure MySQL service is running.")
        print("  Windows: net start MySQL84")
        sys.exit(1)

    log.info("Schema created/verified (tables: trades, journal_entries, forward_test_trades, daily_metrics, bot_state)")

    # Migrate existing JSON data
    total = 0
    total += migrate_performance_state(db)
    total += migrate_trade_journal(db)
    total += migrate_forward_test(db)

    print()
    print(f"Setup complete. {total} total records migrated to MySQL.")
    print("The bot will now use MySQL for all new data. JSON files are kept as backup.")

    # Verify with a quick query
    wr = db.get_win_rate(period_days=365)
    if wr is not None:
        print(f"Current win rate (all time): {wr:.1f}%")
    else:
        print("No trade history in DB yet — win rate will populate as trades close.")


if __name__ == "__main__":
    main()
