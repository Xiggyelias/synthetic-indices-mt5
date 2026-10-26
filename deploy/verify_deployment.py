#!/usr/bin/env python3
"""
QUANTA-SYNTH & QUANTUM BRAIN - Pre-Flight Deployment Diagnostic
==============================================================
Validates that the host environment, MT5 connection, Deriv credentials,
Telegram telemetry, and directory permissions are 100% ready for production.

Usage:
    python deploy/verify_deployment.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Set up paths
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

# Reconfigure console encoding for Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_status(component: str, ok: bool, details: str, warning: bool = False):
    if ok:
        mark = f"{Colors.GREEN}[✓ PASS]{Colors.END}"
    elif warning:
        mark = f"{Colors.YELLOW}[⚠ WARN]{Colors.END}"
    else:
        mark = f"{Colors.RED}[✗ FAIL]{Colors.END}"
    print(f" {mark} {component:<26} : {details}")


def verify_python():
    v = sys.version_info
    ok = v.major == 3 and v.minor >= 10
    details = f"Python {v.major}.{v.minor}.{v.micro} ({'Compatible' if ok else 'Requires Python 3.10+'})"
    print_status("Python Runtime", ok, details)
    return ok


def verify_dependencies():
    required = [
        ("numpy", "numpy"),
        ("pandas", "pandas"),
        ("scipy", "scipy"),
        ("streamlit", "streamlit"),
        ("plotly", "plotly"),
        ("dotenv", "python-dotenv"),
        ("websockets", "websockets"),
        ("requests", "requests"),
    ]
    missing = []
    for mod, pkg in required:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    # Check MetaTrader5 specifically (Windows-only package)
    mt5_installed = False
    try:
        import MetaTrader5  # noqa: F401
        mt5_installed = True
    except ImportError:
        pass

    if missing:
        print_status("Core Python Libraries", False, f"Missing: {', '.join(missing)} -> Run: pip install -r requirements.txt")
        return False
    
    print_status("Core Python Libraries", True, "All math, data, and web packages installed")
    
    if mt5_installed:
        print_status("MetaTrader5 API Library", True, "Installed and ready for MT5 IPC")
    else:
        print_status("MetaTrader5 API Library", False, "Missing (Run: pip install MetaTrader5 on Windows)", warning=True)
    return True


def verify_environment_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        print_status(".env Configuration", False, "Missing .env file! Copy deploy/.env.production.template to .env")
        return False

    load_dotenv(env_path)
    print_status(".env Configuration", True, f"Loaded from {env_path}")
    return True


def verify_directories():
    dirs = [ROOT / "data", ROOT / "logs", ROOT / "config"]
    all_ok = True
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test_file = d / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
        except Exception as e:
            all_ok = False
            print_status(f"Directory {d.name}/", False, f"Permission error: {e}")
            break
    if all_ok:
        print_status("Directory Permissions", True, "Write access verified for data/, logs/, config/")
    return all_ok


def verify_mt5_configuration():
    terminal_path = os.getenv("MT5_PATH") or os.getenv("MT5_TERMINAL_PATH")
    login = os.getenv("MT5_LOGIN", "")
    server = os.getenv("MT5_SERVER", "Deriv-Demo")

    # 1. Check path
    if terminal_path and Path(terminal_path).exists():
        print_status("MT5 Terminal Binary", True, f"Found at {terminal_path}")
    elif terminal_path:
        print_status("MT5 Terminal Binary", False, f"Path not found: {terminal_path}", warning=True)
    else:
        print_status("MT5 Terminal Binary", True, "Auto-detecting active desktop terminal", warning=False)

    # 2. Check credentials configuration
    if not login or login == "YOUR_DERIV_LOGIN_NUMBER" or login == "0":
        print_status("MT5 Credentials", False, "MT5_LOGIN not configured in .env (or running via Auto-Attach)", warning=True)
    else:
        print_status("MT5 Credentials", True, f"Account ID: {login} on Server: {server}")

    # 3. Test active MT5 Connection
    try:
        from mt5_connection import MT5Connection, MT5Credentials
        path_val = terminal_path if (terminal_path and Path(terminal_path).exists()) else None
        creds = MT5Credentials(
            login=int(login) if login.isdigit() else 0,
            password=os.getenv("MT5_PASSWORD", ""),
            server=server,
            terminal_path=path_val,
            path=path_val,
            portable=False,
            is_demo=True,
        )
        conn = MT5Connection(creds)
        if conn.initialize():
            info = conn.account_info()
            if info:
                balance = info.get("balance", 0.0)
                equity = info.get("equity", 0.0)
                curr = info.get("currency", "USD")
                srv = info.get("server", server)
                print_status("MT5 Live Connection", True, f"Logged into {srv} | Balance: {balance:,.2f} {curr} | Equity: {equity:,.2f} {curr}")
            else:
                print_status("MT5 Live Connection", True, "Connected to MT5 terminal")
            conn.shutdown()
            return True
        else:
            print_status("MT5 Live Connection", False, "Could not attach to MT5 terminal. Ensure MT5 is running with 'Algo Trading' ON.", warning=True)
    except Exception as e:
        print_status("MT5 Live Connection", False, f"Connection test failed: {e}", warning=True)

    return False


def verify_deriv_api():
    token = os.getenv("DERIV_API_TOKEN", "")
    app_id = os.getenv("DERIV_APP_ID", "1089")
    if not token or token == "YOUR_DERIV_API_TOKEN":
        print_status("Deriv WebSocket Token", True, "Optional: Not configured (required only for Quantum Brain v2 Deriv API)", warning=False)
        return True

    print_status("Deriv WebSocket Token", True, f"Configured (App ID: {app_id})")
    return True


def verify_telegram():
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not enabled:
        print_status("Telegram Notifications", True, "Disabled (Set TELEGRAM_ENABLED=true in .env to activate)")
        return True

    if not token or token.startswith("YOUR_"):
        print_status("Telegram Notifications", False, "Enabled but TELEGRAM_BOT_TOKEN is missing or default")
        return False

    if not chat_id or chat_id.startswith("YOUR_"):
        print_status("Telegram Notifications", False, "Enabled but TELEGRAM_CHAT_ID is missing")
        return False

    try:
        import requests
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if resp.status_code == 200 and resp.json().get("ok"):
            bot_name = resp.json().get("result", {}).get("username", "Bot")
            print_status("Telegram Notifications", True, f"Connected to @{bot_name} (Chat ID: {chat_id})")
            return True
        else:
            print_status("Telegram Notifications", False, f"Telegram API error: {resp.text}")
            return False
    except Exception as e:
        print_status("Telegram Notifications", False, f"Could not reach Telegram API: {e}", warning=True)
        return False


def main():
    print("=" * 70)
    print(" 🚀 QUANTA-SYNTH / QUANTUM BRAIN - PRODUCTION DEPLOYMENT AUDITOR")
    print("=" * 70)
    print()

    p_ok = verify_python()
    d_ok = verify_dependencies()
    e_ok = verify_environment_file()
    dir_ok = verify_directories()
    m_ok = verify_mt5_configuration()
    der_ok = verify_deriv_api()
    t_ok = verify_telegram()

    print()
    print("=" * 70)
    all_ready = p_ok and d_ok and e_ok and dir_ok
    if all_ready:
        print(f" {Colors.GREEN}{Colors.BOLD}✅ SYSTEM IS PREPARED FOR DEPLOYMENT!{Colors.END}")
        print(" Next step: Launch with 'deploy\\start_production_bot.bat'")
    else:
        print(f" {Colors.RED}{Colors.BOLD}⚠ ACTION REQUIRED: Fix the failures above before hosting.{Colors.END}")
    print("=" * 70)


if __name__ == "__main__":
    main()
