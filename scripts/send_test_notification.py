from __future__ import annotations

import argparse
from datetime import date, time
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.notifications import NotificationContext, send_notification


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a BookGym test notification.")
    parser.add_argument(
        "--subject",
        default="BookGym test notification",
        help="Notification subject line.",
    )
    parser.add_argument(
        "--body",
        default="This is a BookGym test notification.",
        help="Notification body text.",
    )
    parser.add_argument("--class-name", help="Optional class name for a booking-style email.")
    parser.add_argument("--status", help="Optional booking status such as booked, failed, or dry_run.")
    parser.add_argument("--class-date", help="Optional class date in YYYY-MM-DD format.")
    parser.add_argument("--class-time", help="Optional class time in HH:MM format.")
    parser.add_argument("--seat-label", default="", help="Optional seat label to include.")
    parser.add_argument("--dry-run", action="store_true", help="Mark the sample as a dry run.")
    parser.add_argument("--screenshot-path", default="", help="Optional screenshot path to include.")
    return parser.parse_args()


def parse_optional_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def parse_optional_time(value: str | None) -> time | None:
    if not value:
        return None
    return time.fromisoformat(value)


def main() -> int:
    args = parse_args()
    context = None
    if any([args.class_name, args.status, args.class_date, args.class_time, args.dry_run, args.screenshot_path]):
        context = NotificationContext(
            class_name=args.class_name or "",
            status=args.status or "",
            class_date=parse_optional_date(args.class_date),
            class_time=parse_optional_time(args.class_time),
            seat_label=args.seat_label,
            is_dry_run=args.dry_run,
            screenshot_path=args.screenshot_path,
        )
    send_notification(subject=args.subject, body=args.body, context=context)
    print("Notification dispatch completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
