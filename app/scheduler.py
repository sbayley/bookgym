from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlmodel import select

from app.booking_pauses import booking_pause_for_date
from app.config import get_settings
from app.db import get_session, init_db
from app.discovery import refresh_discovery
from app.email_approval import process_approval_replies, send_due_approval_requests
from app.job_sources import DRY_RUN_SELECTION_RULE_NAME, MANUAL_SELECTION_RULE_NAME
from app.jobs.book_class import book_next_available_class
from app.models import BookingJob, BookingStatus
from app.scheduler_runtime import update_scheduler_runtime
from app.time_utils import local_now


def get_next_due_job() -> BookingJob | None:
    init_db()
    with get_session() as session:
        queued_jobs = session.exec(
            select(BookingJob).where(
                BookingJob.rule_name.in_([MANUAL_SELECTION_RULE_NAME, DRY_RUN_SELECTION_RULE_NAME]),
                BookingJob.status.in_([BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN])
            ).order_by(BookingJob.expected_run_at)
        ).all()
        next_job = next(
            (
                job
                for job in queued_jobs
                if booking_pause_for_date(session, job.class_date, user_id=job.user_id) is None
            ),
            None,
        )

    return next_job


def describe_next_due_job() -> str:
    next_job = get_next_due_job()
    if next_job is None:
        return "No selected booking jobs are scheduled."

    return (
        f"Next due job: id={next_job.id}, "
        f"source={'manual_dry_run' if next_job.rule_name == DRY_RUN_SELECTION_RULE_NAME else 'manual_selection'}, "
        f"class_date={next_job.class_date.isoformat()}, "
        f"expected_run_at={next_job.expected_run_at.isoformat(sep=' ', timespec='seconds')}, "
        f"status={next_job.status.value}"
    )


def refresh_discovery_safely() -> str:
    started_at = local_now()
    update_scheduler_runtime(
        detail="Refreshing discovery ranges.",
        last_discovery_refresh_started_at=started_at,
    )
    try:
        summary = refresh_discovery(stale_only=True)
    except Exception as exc:
        print(f"Discovery refresh failed: {exc}")
        update_scheduler_runtime(
            detail="Discovery refresh failed.",
            last_discovery_refresh_finished_at=local_now(),
            last_error=str(exc),
        )
        return f"Discovery refresh failed: {exc}"

    message = (
        f"Discovery refresh complete: scanned {summary.scanned_ranges} range(s), "
        f"matched {summary.matched_classes} class observation(s)."
    )
    print(message)
    update_scheduler_runtime(
        detail=message,
        last_discovery_refresh_finished_at=local_now(),
    )
    return message


def _heartbeat() -> None:
    next_job = get_next_due_job()
    detail = "Scheduler heartbeat: watching for due booking jobs."
    if next_job is not None:
        detail = (
            "Scheduler heartbeat: "
            f"next job {next_job.id} due at {next_job.expected_run_at.isoformat(sep=' ', timespec='seconds')}."
        )
    update_scheduler_runtime(
        detail=detail,
        next_due_job_id=next_job.id if next_job is not None else None,
        next_due_expected_run_at=next_job.expected_run_at if next_job is not None else None,
    )


def _run_booking_worker_tick() -> None:
    poll_started_at = local_now()
    next_job_before = get_next_due_job()
    update_scheduler_runtime(
        detail=(
            "Booking worker polling due jobs."
            if next_job_before is None
            else f"Booking worker polling. Next queued job is {next_job_before.id}."
        ),
        last_booking_poll_at=poll_started_at,
        next_due_job_id=next_job_before.id if next_job_before is not None else None,
        next_due_expected_run_at=(
            next_job_before.expected_run_at if next_job_before is not None else None
        ),
    )
    try:
        result = book_next_available_class()
    except Exception as exc:
        update_scheduler_runtime(
            detail="Booking worker tick failed.",
            last_booking_run_finished_at=local_now(),
            last_error=str(exc),
        )
        raise
    next_job_after = get_next_due_job()
    if result.get("action") == "ran_job":
        update_scheduler_runtime(
            detail=(
                f"Booking worker ran job {result['job_id']}."
                if next_job_after is None
                else (
                    f"Booking worker ran job {result['job_id']}. "
                    f"Next queued job is {next_job_after.id}."
                )
            ),
            last_booking_run_started_at=poll_started_at,
            last_booking_run_finished_at=local_now(),
            next_due_job_id=next_job_after.id if next_job_after is not None else None,
            next_due_expected_run_at=(
                next_job_after.expected_run_at if next_job_after is not None else None
            ),
        )
        return

    update_scheduler_runtime(
        detail=(
            str(result.get("message") or "No due booking jobs were ready.")
            if next_job_after is None
            else (
                f"{str(result.get('message') or 'No due booking jobs were ready.')} "
                f"Next queued job is {next_job_after.id}."
            )
        ),
        last_booking_run_finished_at=local_now(),
        next_due_job_id=next_job_after.id if next_job_after is not None else None,
        next_due_expected_run_at=(
            next_job_after.expected_run_at if next_job_after is not None else None
        ),
    )


def _run_email_approval_tick() -> None:
    reply_result = process_approval_replies()
    send_result = send_due_approval_requests()
    if reply_result.get("rejected") or send_result.get("sent"):
        print(
            "Email approval tick: "
            f"sent={send_result.get('sent', 0)}, "
            f"processed={reply_result.get('processed', 0)}, "
            f"rejected={reply_result.get('rejected', 0)}"
        )


def run_scheduler() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone_name)

    scheduler.add_job(
        refresh_discovery_safely,
        "interval",
        minutes=settings.discovery_refresh_minutes,
        id="refresh-discovery",
        max_instances=1,
    )
    scheduler.add_job(
        _run_booking_worker_tick,
        "interval",
        seconds=settings.booking_worker_interval_seconds,
        id="run-booking-worker",
        max_instances=1,
    )
    scheduler.add_job(
        _heartbeat,
        "interval",
        seconds=settings.scheduler_heartbeat_seconds,
        id="scheduler-heartbeat",
        max_instances=1,
    )
    if settings.email_approval_enabled:
        scheduler.add_job(
            _run_email_approval_tick,
            "interval",
            seconds=settings.email_approval_poll_seconds,
            id="email-approval",
            max_instances=1,
        )

    refresh_discovery_safely()
    if settings.email_approval_enabled:
        _run_email_approval_tick()
    _heartbeat()
    print(describe_next_due_job())
    print("Scheduler started. Press Ctrl+C to stop.")
    scheduler.start()
