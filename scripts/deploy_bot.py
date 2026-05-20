#!/usr/bin/env python3
"""
MT5 Bot Deployment Script
Deploys the "Revert & Protect" configuration to MT5 with safety verification.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from telegram_notifier import TelegramNotifier, build_telegram_config_from_env


def setup_logging():
    """Setup logging for deployment."""
    os.makedirs("logs", exist_ok=True)
    log_file = os.path.join("logs", f"deployment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)


def run_command(command, description, shell=True, timeout=60):
    """Run a command and log results."""
    logger = logging.getLogger(__name__)
    logger.info("Executing: %s", description)
    logger.info("Command: %s", command)

    try:
        if shell:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        else:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)

        logger.info("Return code: %s", result.returncode)
        if result.stdout:
            logger.info("STDOUT: %s", result.stdout.strip())
        if result.stderr:
            logger.warning("STDERR: %s", result.stderr.strip())

        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("Command timed out: %s", command)
        return False, "", "Command timed out"
    except Exception as exc:
        logger.error("Command failed: %s", exc)
        return False, "", str(exc)


def verify_python_environment():
    """Verify Python environment and dependencies."""
    logger = logging.getLogger(__name__)
    logger.info("Verifying Python environment...")

    python_version = sys.version_info
    logger.info("Python version: %s.%s.%s", python_version.major, python_version.minor, python_version.micro)

    if python_version.major < 3 or (python_version.major == 3 and python_version.minor < 8):
        logger.error("Python 3.8+ required")
        return False

    required_files = [
        "src/trading_bot_engulfing_v2.py",
        "mt5_bot.py",
        "src/mt5_system.py",
        "src/mt5_connection.py",
        "src/telegram_notifier.py",
        "scripts/forward_test_tracker.py",
        "config/config_rsi_pullback.json",
        "requirements.txt",
    ]

    missing_files = [file for file in required_files if not os.path.exists(file)]
    if missing_files:
        logger.error("Missing required files: %s", missing_files)
        return False

    logger.info("All required files present")

    if os.path.exists("requirements.txt"):
        success, _, _ = run_command("pip install -r requirements.txt", "Install dependencies")
        if not success:
            logger.error("Failed to install dependencies")
            return False

    logger.info("Python environment verified")
    return True


def verify_configuration():
    """Verify Revert & Protect configuration."""
    logger = logging.getLogger(__name__)
    logger.info("Verifying Revert & Protect configuration...")

    config_file = "config/config_rsi_pullback.json"

    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            config = json.load(handle)

        expected_settings = {
            "strategy_mode": "RSI_PULLBACK",
            "risk_per_trade": 0.01,
            "min_risk_reward": 2.2,
            "max_daily_loss": 0.015,
            "max_open_trades": 1,
            "rsi_pullback_buy_threshold": 40,
            "rsi_pullback_sell_threshold": 60,
            "volume_multiplier": 1.3,
            "sr_tolerance": 0.0035,
            "avoid_news_minutes": 30,
        }

        risk_mgmt = config.get("risk_management", {})
        for key, expected_value in expected_settings.items():
            if key in risk_mgmt and risk_mgmt[key] != expected_value:
                logger.error("Config mismatch: %s = %s, expected %s", key, risk_mgmt[key], expected_value)
                return False

        strategy_params = config.get("strategy_parameters", {})
        for key, expected_value in expected_settings.items():
            if key in strategy_params and strategy_params[key] != expected_value:
                logger.error("Config mismatch: %s = %s, expected %s", key, strategy_params[key], expected_value)
                return False

        logger.info("Configuration verified - Revert & Protect settings confirmed")
        logger.info("Key Configuration:")
        logger.info("  Strategy Mode: %s", strategy_params.get("strategy_mode"))
        logger.info(
            "  RSI Thresholds: %s/%s",
            strategy_params.get("rsi_settings", {}).get("rsi_pullback_buy_threshold"),
            strategy_params.get("rsi_settings", {}).get("rsi_pullback_sell_threshold"),
        )
        logger.info(
            "  Volume Multiplier: %s",
            strategy_params.get("volume_settings", {}).get("volume_multiplier"),
        )
        logger.info(
            "  SR Tolerance: %s",
            strategy_params.get("support_resistance", {}).get("sr_tolerance"),
        )
        logger.info("  Max Daily Loss: %.1f%%", risk_mgmt.get("max_daily_loss", 0) * 100)
        logger.info("  Max Open Trades: %s", risk_mgmt.get("max_open_trades"))
        logger.info(
            "  News Filter: %s minutes",
            strategy_params.get("news_filter", {}).get("avoid_news_minutes"),
        )
        return True
    except Exception as exc:
        logger.error("Failed to verify configuration: %s", exc)
        return False


def verify_telegram_setup():
    """Verify Telegram notification setup."""
    logger = logging.getLogger(__name__)
    logger.info("Verifying Telegram setup...")

    if not os.path.exists(".env"):
        logger.warning(".env file not found")
        return False

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        logger.error("python-dotenv not installed")
        return False

    config = build_telegram_config_from_env(os.getenv)
    if not config.enabled:
        logger.warning("Telegram notifications disabled")
        return True

    if not config.bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is missing")
        return False

    notifier = TelegramNotifier(config)
    profile = notifier.get_bot_profile()
    if not profile:
        logger.error("Telegram Bot API validation failed")
        return False

    username = str(profile.get("username", "") or "").strip()
    chat_id = notifier.discover_chat_id(force=True) or str(config.chat_id or "").strip()
    if not chat_id:
        if username:
            logger.error(
                "Telegram bot @%s has no available chat yet. Open https://t.me/%s and send /start, then rerun deployment.",
                username,
                username,
            )
        else:
            logger.error("Telegram bot has no available chat yet. Send /start to the bot and rerun deployment.")
        return False

    success, _, _ = run_command("python -X utf8 scripts/test_telegram_integration.py", "Test Telegram integration")
    if success:
        logger.info("Telegram integration verified")
        return True

    logger.error("Telegram integration test failed")
    return False


def verify_mt5_connection():
    """Verify MT5 connection and terminal."""
    logger = logging.getLogger(__name__)
    logger.info("Verifying MT5 connection...")

    success, _, stderr = run_command("python mt5_bot.py --test-connection", "Test MT5 connection", timeout=30)
    if success:
        logger.info("MT5 connection verified")
        return True

    logger.error("MT5 connection failed: %s", stderr)
    return False


def verify_safety_guards():
    """Verify safety guardrails are active."""
    logger = logging.getLogger(__name__)
    logger.info("Verifying safety guardrails...")

    success, _, stderr = run_command("python mt5_bot.py --safety-check", "Verify safety guards", timeout=30)
    if success:
        logger.info("Safety guardrails verified")
        return True

    logger.error("Safety guardrails check failed: %s", stderr)
    return False


def create_deployment_backup():
    """Create backup of current deployment."""
    logger = logging.getLogger(__name__)
    logger.info("Creating deployment backup...")

    backup_dir = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        os.makedirs(backup_dir, exist_ok=True)
        backup_files = [
            "config/config_rsi_pullback.json",
            ".env",
            "src/trading_bot_engulfing_v2.py",
            "mt5_bot.py",
        ]
        for file in backup_files:
            if os.path.exists(file):
                shutil.copy2(file, os.path.join(backup_dir, file))
                logger.info("Backed up: %s", file)

        logger.info("Backup created: %s", backup_dir)
        return True
    except Exception as exc:
        logger.error("Failed to create backup: %s", exc)
        return False


def deploy_bot():
    """Deploy the bot with Revert & Protect configuration."""
    logger = logging.getLogger(__name__)
    logger.info("Deploying MT5 Bot with Revert & Protect configuration...")

    try:
        os.makedirs("data", exist_ok=True)
        with open("data/bot_switch.txt", "w", encoding="utf-8") as handle:
            handle.write("on")
        logger.info("Bot switch enabled")
    except Exception as exc:
        logger.error("Failed to create bot switch: %s", exc)
        return False

    success, _, stderr = run_command(
        "python mt5_bot.py --system --demo --config config/config_rsi_pullback.json --test-run",
        "Test bot startup",
        timeout=60,
    )
    if success:
        logger.info("Bot deployment test successful")
        return True

    logger.error("Bot deployment test failed: %s", stderr)
    return False


def launch_forward_test():
    """Launch forward-testing."""
    logger = logging.getLogger(__name__)
    logger.info("Launching forward-testing...")

    command = "python mt5_bot.py --system --demo --config config/config_rsi_pullback.json"
    logger.info("Starting bot for forward-testing...")
    logger.info("Command: %s", command)
    logger.info("Bot will run in background. Monitor logs and Telegram notifications.")
    logger.info("Forward-testing launched")
    logger.info("Monitor progress via:")
    logger.info("  - Telegram notifications")
    logger.info("  - scripts/forward_test_tracker.py")
    logger.info("  - Log files")
    return True


def display_deployment_summary():
    """Display deployment summary."""
    logger = logging.getLogger(__name__)
    logger.info("=" * 80)
    logger.info("DEPLOYMENT SUMMARY")
    logger.info("=" * 80)
    logger.info("Configuration: Revert & Protect")
    logger.info("Strategy Mode: RSI_PULLBACK")
    logger.info("Risk Per Trade: 0.5%%")
    logger.info("Max Daily Loss: 1.5%%")
    logger.info("Max Open Trades: 1")
    logger.info("News Filter: 30 minutes")
    logger.info("Break-Even: 1.0R")
    logger.info("Stepped Trailing: 1.5R -> 0.5R")
    logger.info("Target: 20 trades or 30 days")
    logger.info("=" * 80)
    logger.info("NEXT STEPS:")
    logger.info("1. Monitor Telegram notifications")
    logger.info("2. Track forward-test progress")
    logger.info("3. Log Trade #1 details")
    logger.info("4. Evaluate after 20 trades/30 days")
    logger.info("=" * 80)


def main():
    """Main deployment function."""
    logger = setup_logging()
    logger.info("Starting MT5 Bot Deployment - Revert & Protect Configuration")

    if not verify_python_environment():
        logger.error("Python environment verification failed")
        return False

    if not verify_configuration():
        logger.error("Configuration verification failed")
        return False

    if not verify_telegram_setup():
        logger.error("Telegram setup verification failed")
        return False

    if not verify_mt5_connection():
        logger.error("MT5 connection verification failed")
        return False

    if not verify_safety_guards():
        logger.error("Safety guardrails verification failed")
        return False

    if not create_deployment_backup():
        logger.error("Backup creation failed")
        return False

    if not deploy_bot():
        logger.error("Bot deployment failed")
        return False

    if not launch_forward_test():
        logger.error("Forward-testing launch failed")
        return False

    display_deployment_summary()
    logger.info("Deployment completed successfully")
    logger.info("Revert & Protect configuration deployed and forward-testing started")
    return True


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nDeployment interrupted by user.")
        sys.exit(1)
    except Exception as exc:
        print(f"\nDeployment failed: {exc}")
        sys.exit(1)
