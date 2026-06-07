from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path


def is_weekly_market_open(now_utc: datetime | None = None) -> tuple[bool, str]:
    now = now_utc or datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday=0, Sunday=6

    if weekday == 5:
        return False, "weekly market close (Saturday UTC)"
    if weekday == 6 and now.hour < 21:
        return False, "weekly market close until Sunday 21:00 UTC"
    if weekday == 4 and now.hour >= 21:
        return False, "weekly market close from Friday 21:00 UTC"

    return True, "market open"


def is_switch_on(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, "switch file missing; defaulting to ON"

    value = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    if value == "off":
        return False, "bot switch is OFF"
    return True, "bot switch is ON"


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard bot launches for watchdog scripts.")
    parser.add_argument("--switch-path", default="data/bot_switch.txt")
    parser.add_argument(
        "--mode",
        choices=["ready", "status"],
        default="ready",
        help="ready returns exit code only; status prints details and also uses exit code.",
    )
    args = parser.parse_args()

    switch_on, switch_reason = is_switch_on(Path(args.switch_path))
    market_open, market_reason = is_weekly_market_open()
    ready = switch_on and market_open

    if args.mode == "status":
        print(f"switch: {'ON' if switch_on else 'OFF'} ({switch_reason})")
        print(f"market: {'OPEN' if market_open else 'CLOSED'} ({market_reason})")
        print(f"ready: {'YES' if ready else 'NO'}")

    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
