#!/usr/bin/env python3
"""
Test Telegram Integration Script
Verifies Telegram notifications work correctly before deployment.
"""

import os
import sys
from datetime import datetime

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add current directory and src to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from forward_test_tracker import ForwardTestTracker
from telegram_notifier import TelegramNotifier, build_telegram_config_from_env


def validate_telegram_setup(notifier: TelegramNotifier) -> bool:
    """Validate bot token and discover chat id when possible."""
    profile = notifier.get_bot_profile()
    if not profile:
        print("Telegram Bot API validation failed. Check TELEGRAM_BOT_TOKEN.")
        return False

    username = str(profile.get("username", "") or "").strip()
    if username:
        print(f"Bot token verified for @{username}")
    else:
        print("Bot token verified")

    chat_id = notifier.discover_chat_id(force=True)
    if chat_id:
        print(f"Chat ID ready: {chat_id}")
        return True

    print("No Telegram chat is available for this bot yet.")
    if username:
        print(f"Open https://t.me/{username} and send /start, then rerun this test.")
    else:
        print("Open the bot in Telegram and send /start, then rerun this test.")
    print("If you already know the chat id, set TELEGRAM_CHAT_ID in .env.")
    return False


def test_telegram_integration() -> bool:
    """Test all Telegram notification types."""
    print("Testing Telegram Integration...")
    load_dotenv()

    config = build_telegram_config_from_env(os.getenv)
    if not config.enabled:
        print("Telegram not enabled. Set TELEGRAM_ENABLED=true in environment.")
        return False
    if not config.bot_token:
        print("Telegram bot token missing. Set TELEGRAM_BOT_TOKEN.")
        return False

    notifier = TelegramNotifier(config)
    if not validate_telegram_setup(notifier):
        return False

    print("Test 1: Basic message...")
    success = notifier._send_message("Telegram Integration Test - Basic Message")
    print(f"Basic message: {'Success' if success else 'Failed'}")
    if not success:
        return False

    print("Test 2: Trade execution notification...")
    trade_data = {
        "direction": "BUY",
        "entry_price": "1.08567",
        "stop_loss": "1.08345",
        "take_profit": "1.09012",
        "risk_reward": "2.2",
        "volume_multiplier": "1.3",
        "rsi_value": "38.5",
        "rsi_pullback": "40",
        "sr_tolerance": "0.0035",
        "daily_loss_pct": "0.0",
        "open_trades": "1",
        "news_pause_status": "Active",
        "slippage_pips": "0.8",
        "spread_points": "3",
        "trades_completed": "1",
    }
    success = notifier.send_trade_execution(trade_data)
    print(f"Trade execution: {'Success' if success else 'Failed'}")
    if not success:
        return False

    print("Test 3: Trade exit notification...")
    exit_data = {
        "direction": "BUY",
        "entry_price": "1.08567",
        "exit_price": "1.09012",
        "profit_loss": 45.50,
        "r_multiple": "2.2",
        "exit_reason": "TP",
        "duration_minutes": "145",
        "slippage_pips": "0.8",
        "trades_completed": "1",
    }
    success = notifier.send_trade_exit(exit_data)
    print(f"Trade exit: {'Success' if success else 'Failed'}")
    if not success:
        return False

    print("Test 4: Alert notification...")
    success = notifier.send_alert("HIGH_SLIPPAGE", "Slippage 2.1 pips exceeds 1.5 pips limit", "WARNING")
    print(f"Alert: {'Success' if success else 'Failed'}")
    if not success:
        return False

    print("Test 5: Milestone notification...")
    progress_data = {
        "total_trades": "10",
        "win_rate": 0.458,
        "profit_factor": 1.12,
        "expectancy": 3.40,
        "total_pnl": 340.00,
        "avg_slippage": 0.8,
        "max_drawdown": 0.0423,
        "trades_completed": "10",
        "test_days": "15",
    }
    success = notifier.send_milestone("Trade #10 Reached", progress_data)
    print(f"Milestone: {'Success' if success else 'Failed'}")
    if not success:
        return False

    print("\nTelegram Integration Test Complete.")
    print("Check your Telegram chat for all test messages.")
    return True


def test_forward_test_tracker_integration() -> bool:
    """Test forward-test tracker with Telegram integration."""
    print("\nTesting Forward-Test Tracker Integration...")
    load_dotenv()

    config = build_telegram_config_from_env(os.getenv)
    if not config.enabled:
        print("Telegram not enabled for tracker test.")
        return False

    notifier = TelegramNotifier(config)
    if not validate_telegram_setup(notifier):
        return False
    tracker = ForwardTestTracker(telegram_notifier=notifier)

    print("Testing trade entry with alerts...")
    entry_data = {
        "ticket": 12345,
        "direction": "BUY",
        "entry_time": datetime.now().isoformat(),
        "entry_price": 1.08567,
        "slippage_pips": 2.1,
        "spread": 3,
        "news_affected": False,
    }
    tracker.record_trade_entry(entry_data)
    print("Trade entry recorded.")

    print("Testing trade exit with milestones...")
    exit_data = {
        "ticket": 12345,
        "exit_time": datetime.now().isoformat(),
        "exit_price": 1.09012,
        "profit_loss": 45.50,
        "exit_reason": "TP",
    }
    tracker.record_trade_exit(exit_data)
    print("Trade exit recorded.")

    print("Testing daily metrics with alerts...")
    daily_data = {
        "date": datetime.now().isoformat(),
        "trades_taken": 1,
        "total_pnl": -200.00,
        "total_slippage": 2.1,
        "max_spread": 5,
        "uptime_percentage": 98.5,
        "news_blocks": 0,
        "connection_issues": 2,
    }
    tracker.record_daily_metrics(daily_data)
    print("Daily metrics recorded.")

    print("\nForward-Test Tracker Integration Test Complete.")
    return True


def create_env_template() -> bool:
    """Create .env template if it doesn't exist."""
    env_file = ".env"
    template_file = ".env.template"

    if not os.path.exists(env_file) and os.path.exists(template_file):
        print(f"Copying {template_file} to {env_file}...")
        with open(template_file, "r", encoding="utf-8") as handle:
            template_content = handle.read()
        with open(env_file, "w", encoding="utf-8") as handle:
            handle.write(template_content)
        print(f"{env_file} created. Please edit it with your Telegram credentials.")
        return True

    return False


if __name__ == "__main__":
    print("MT5 Engulfing Bot - Telegram Integration Test")
    print("=" * 50)

    create_env_template()

    if not os.path.exists(".env"):
        print(".env file not found. Please create it with your Telegram credentials:")
        print("TELEGRAM_ENABLED=true")
        print("TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("TELEGRAM_CHAT_ID=your_chat_id_here")
        sys.exit(1)

    success = False
    try:
        if test_telegram_integration():
            print("\nBasic Telegram integration working.")
            if test_forward_test_tracker_integration():
                print("\nForward-test tracker integration working.")
                print("\nALL TESTS PASSED")
                print("Your Telegram notification system is ready for deployment.")
                print("\nNext steps:")
                print("1. Deploy bot to VPS")
                print("2. Start forward-testing")
                print("3. Monitor Telegram notifications")
                success = True
            else:
                print("\nForward-test tracker integration failed.")
        else:
            print("\nBasic Telegram integration failed.")
    except KeyboardInterrupt:
        print("\nTest interrupted by user.")
        sys.exit(1)
    except Exception as exc:
        print(f"\nTest failed with error: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    sys.exit(0 if success else 1)
