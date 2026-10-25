#!/usr/bin/env python3
"""
Runtime control helper for the MT5 trading bot.

Modes:
  - on:  turn the bot switch on and start the watchdog if nothing is running
  - off: turn the bot switch off, stop the active bot process, and close MT5
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import psutil


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SWITCH_PATH = ROOT / "data" / "bot_switch.txt"
EXCLUDE_FLAGS = {
    "--status-check",
    "--backtest",
    "--backtest-v2",
    "--safety-check",
    "--close-all",
    "--emergency-status",
}


def _process_cmdline(proc: psutil.Process) -> str:
    try:
        cmdline = proc.info.get("cmdline") or proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""
    if isinstance(cmdline, str):
        return cmdline
    return " ".join(str(part) for part in cmdline)


def _is_bot_process(proc: psutil.Process) -> bool:
    try:
        name = (proc.info.get("name") or proc.name() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if "python" not in name:
        return False

    cmd = _process_cmdline(proc)
    if ("mt5_bot.py" not in cmd) and ("synthetic_bot.py" not in cmd):
        return False
    if any(flag in cmd for flag in EXCLUDE_FLAGS):
        return False
    return True


def _is_watchdog_process(proc: psutil.Process) -> bool:
    cmd = _process_cmdline(proc).lower()
    if not cmd:
        return False
    return "run_bot_forever.bat" in cmd


def _iter_processes() -> list[psutil.Process]:
    return list(psutil.process_iter(["pid", "name", "cmdline"]))


def _write_switch(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip().lower(), encoding="utf-8")


def _terminate_processes(processes: list[psutil.Process], timeout: float = 5.0) -> list[str]:
    if not processes:
        return []

    for proc in processes:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gone, alive = psutil.wait_procs(processes, timeout=timeout)

    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if alive:
        gone_after_kill, _ = psutil.wait_procs(alive, timeout=timeout)
        gone.extend(gone_after_kill)

    stopped = []
    for proc in gone:
        try:
            stopped.append(f"{proc.pid}:{proc.name()}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            stopped.append(f"{proc.pid}:<exited>")
    return stopped


def _collect_runtime_state() -> tuple[list[psutil.Process], list[psutil.Process], list[psutil.Process]]:
    bot_processes: list[psutil.Process] = []
    watchdog_processes: list[psutil.Process] = []
    mt5_processes: list[psutil.Process] = []

    for proc in _iter_processes():
        try:
            name = (proc.info.get("name") or proc.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        if _is_bot_process(proc):
            bot_processes.append(proc)
            continue

        if _is_watchdog_process(proc):
            watchdog_processes.append(proc)
            continue

        if name == "terminal64.exe":
            mt5_processes.append(proc)

    return bot_processes, watchdog_processes, mt5_processes


def _start_watchdog() -> None:
    batch_path = ROOT / "scripts" / "run_bot_forever.bat"
    if not batch_path.exists():
        raise FileNotFoundError(f"Watchdog script not found: {batch_path}")

    comspec = os.environ.get("COMSPEC", "cmd.exe")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [comspec, "/c", str(batch_path)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def mode_on(switch_path: Path) -> int:
    _write_switch(switch_path, "on")
    bot_processes, watchdog_processes, _ = _collect_runtime_state()

    if bot_processes or watchdog_processes:
        print("Bot switch set to ON.")
        print("Existing bot/watchdog process already running, so no new launcher was started.")
        return 0

    _start_watchdog()
    print("Bot switch set to ON.")
    print("Watchdog started. The bot will launch automatically when the market gate allows it.")
    return 0


def mode_off(switch_path: Path, stop_mt5: bool) -> int:
    _write_switch(switch_path, "off")
    bot_processes, _, mt5_processes = _collect_runtime_state()

    stopped_bots = _terminate_processes(bot_processes)
    if stopped_bots:
        time.sleep(1.0)

    stopped_mt5: list[str] = []
    if stop_mt5:
        stopped_mt5 = _terminate_processes(mt5_processes)

    print("Bot switch set to OFF.")
    if stopped_bots:
        print("Stopped bot processes:")
        for item in stopped_bots:
            print(f" - {item}")
    else:
        print("No active bot processes were running.")

    if stop_mt5:
        if stopped_mt5:
            print("Closed MT5 terminal processes:")
            for item in stopped_mt5:
                print(f" - {item}")
        else:
            print("No MT5 terminal processes were running.")

    print("Watchdog remains available and will stay idle until the switch is turned back ON.")
    return 0


def mode_status(switch_path: Path) -> int:
    switch_val = "UNSET"
    if switch_path.exists():
        try:
            switch_val = switch_path.read_text(encoding="utf-8").strip().upper()
        except Exception:
            switch_val = "ERROR"

    bot_processes, watchdog_processes, mt5_processes = _collect_runtime_state()

    print("=" * 60)
    print("🤖 BOT RUNTIME CONTROL STATUS")
    print("=" * 60)
    print(f"Switch State:       {switch_val} ({switch_path})")
    print(f"Bot Processes:      {len(bot_processes)} active")
    for proc in bot_processes:
        try:
            print(f"  - PID {proc.pid}: {_process_cmdline(proc)}")
        except Exception:
            print(f"  - PID {proc.pid}")

    print(f"Watchdog Processes: {len(watchdog_processes)} active")
    for proc in watchdog_processes:
        try:
            print(f"  - PID {proc.pid}")
        except Exception:
            pass

    print(f"MT5 Terminal:       {len(mt5_processes)} active")
    for proc in mt5_processes:
        try:
            print(f"  - PID {proc.pid}")
        except Exception:
            pass
    print("=" * 60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage MT5 bot runtime state.")
    parser.add_argument("mode", choices=["on", "off", "status"])
    parser.add_argument("--switch-path", default=str(DEFAULT_SWITCH_PATH))
    parser.add_argument(
        "--leave-mt5-open",
        action="store_true",
        help="Do not close terminal64.exe when switching off.",
    )
    args = parser.parse_args()

    switch_path = Path(args.switch_path)
    if args.mode == "on":
        return mode_on(switch_path)
    elif args.mode == "status":
        return mode_status(switch_path)
    return mode_off(switch_path, stop_mt5=not args.leave_mt5_open)


if __name__ == "__main__":
    raise SystemExit(main())
