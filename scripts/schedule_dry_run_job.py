from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
import sys

from sqlmodel import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import get_session, init_db
from app.job_sources import DRY_RUN_SELECTION_RULE_NAME
from app.models import BookingJob, BookingStatus, DiscoveredClass
from app.notifications import format_recipient_csv, parse_recipient_csv
from app.time_utils import local_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Schedule a scheduler-managed booking dry run that stops before final confirmation.",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=15,
        help="How many minutes from now the dry run should be due. Default: 15.",
    )
    parser.add_argument(
        "--discovered-class-id",
        type=int,
        help="Existing discovered class ID to use.",
    )
    parser.add_argument(
        "--date",
        help="Class date in YYYY-MM-DD format when looking up a class by title/time.",
    )
    parser.add_argument(
        "--class-name",
        help="Class title when looking up a class by date/time.",
    )
    parser.add_argument(
        "--time",
        help="Optional start time in HH:MM format to disambiguate a class lookup.",
    )
    parser.add_argument(
        "--email",
        action="append",
        help="Override email recipients for this dry run. Repeat or pass a comma-separated list.",
    )
    return parser.parse_args()


def parse_class_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid --date value {value!r}. Expected YYYY-MM-DD.") from exc


def resolve_discovered_class(args: argparse.Namespace) -> DiscoveredClass:
    with get_session() as session:
        if args.discovered_class_id is not None:
            discovered_class = session.get(DiscoveredClass, args.discovered_class_id)
            if discovered_class is None:
                raise SystemExit(f"Discovered class {args.discovered_class_id} was not found.")
            return discovered_class

        if not args.date or not args.class_name:
            raise SystemExit(
                "Provide --discovered-class-id, or provide both --date and --class-name."
            )

        statement = select(DiscoveredClass).where(
            DiscoveredClass.class_date == parse_class_date(args.date),
            DiscoveredClass.title == args.class_name,
        )
        matches = list(session.exec(statement).all())
        if args.time:
            matches = [
                item
                for item in matches
                if item.start_time is not None and item.start_time.isoformat(timespec="minutes") == args.time
            ]

        if not matches:
            raise SystemExit("No matching discovered class was found.")
        if len(matches) > 1:
            raise SystemExit(
                "Multiple discovered classes matched. Provide --time or use --discovered-class-id."
            )
        return matches[0]


def parse_override_recipients(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    recipients = []
    for value in values:
        recipients.extend(parse_recipient_csv(value))
    return recipients


def schedule_dry_run_job(
    discovered_class: DiscoveredClass,
    minutes: int,
    notification_email_recipients: list[str] | None = None,
) -> BookingJob:
    now = local_now()
    expected_run_at = now + timedelta(minutes=minutes)
    notification_email_override = (
        format_recipient_csv(notification_email_recipients)
        if notification_email_recipients is not None
        else None
    )

    with get_session() as session:
        existing_job = session.exec(
            select(BookingJob).where(
                BookingJob.rule_name == DRY_RUN_SELECTION_RULE_NAME,
                BookingJob.discovered_class_id == discovered_class.id,
                BookingJob.status.in_([BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN]),
            )
        ).first()

        if existing_job is None:
            job = BookingJob(
                rule_name=DRY_RUN_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=discovered_class.class_date,
                window_start_time=discovered_class.start_time,
                window_end_time=discovered_class.start_time,
                expected_run_at=expected_run_at,
                calendar_event_id=discovered_class.calendar_event_id,
                site_booking_opens_at=discovered_class.site_booking_opens_at,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                last_message="Scheduled dry run from script",
                notification_email_override=notification_email_override,
                notification_sms_override=None,
                created_at=now,
                updated_at=now,
            )
        else:
            job = existing_job
            job.target_match_text = discovered_class.title
            job.class_date = discovered_class.class_date
            job.window_start_time = discovered_class.start_time
            job.window_end_time = discovered_class.start_time
            job.expected_run_at = expected_run_at
            job.calendar_event_id = discovered_class.calendar_event_id
            job.site_booking_opens_at = discovered_class.site_booking_opens_at
            job.status = BookingStatus.PENDING
            job.last_message = "Rescheduled dry run from script"
            job.notification_email_override = notification_email_override
            job.notification_sms_override = None
            job.updated_at = now

        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def main() -> int:
    args = parse_args()
    if args.minutes < 1:
        raise SystemExit("--minutes must be at least 1.")

    init_db()
    discovered_class = resolve_discovered_class(args)
    email_recipients = parse_override_recipients(args.email)
    job = schedule_dry_run_job(
        discovered_class=discovered_class,
        minutes=args.minutes,
        notification_email_recipients=email_recipients,
    )

    print(
        "Scheduled dry-run job "
        f"{job.id} for {job.class_date.isoformat()} "
        f"{job.window_start_time.isoformat(timespec='minutes') if job.window_start_time else 'time-unknown'} "
        f"{job.target_match_text!r} at {job.expected_run_at.isoformat(sep=' ', timespec='seconds')}"
    )
    if discovered_class.id is not None:
        print(f"Discovered class id: {discovered_class.id}")
    if email_recipients is not None:
        print(f"Email override: {email_recipients or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
