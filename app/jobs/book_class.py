from __future__ import annotations

import asyncio
import re
from datetime import datetime, time, timedelta

from playwright.async_api import async_playwright
from sqlmodel import select

from app.adapters.base import BookingResult
from app.adapters.gym_site import ExampleGymAdapter
from app.booking_pauses import booking_pause_for_date, cancel_job_if_paused
from app.config import get_settings
from app.db import get_session, init_db
from app.gym_auth import auth_state_context_kwargs, ensure_auth_state_directory
from app.job_sources import DRY_RUN_SELECTION_RULE_NAME, MANUAL_SELECTION_RULE_NAME
from app.models import BookingEvent, BookingJob, BookingStatus, DiscoveredClass
from app.network_capture import (
    BookingNetworkCapture,
    claim_next_booking_capture,
    complete_next_booking_capture,
)
from app.notifications import NotificationContext, parse_recipient_csv, send_notification
from app.seat_preferences import SeatSelectionPlan, resolve_booking_seat_selection
from app.time_utils import local_now


NOT_YET_OPEN_RETRY_DELAYS_SECONDS = (15, 60, 300)


def _booking_job_is_runnable(job_id: int) -> bool:
    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None or job.status not in [BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN]:
            return False
        return booking_pause_for_date(session, job.class_date, user_id=job.user_id) is None


async def _wait_until_booking_due(expected_run_at: datetime | None) -> None:
    if expected_run_at is None:
        return
    seconds_until_due = max(0.0, (expected_run_at - local_now()).total_seconds())
    if seconds_until_due <= 0:
        return
    print(
        f"Browser session is ready; waiting {seconds_until_due:.1f}s "
        f"for {expected_run_at.isoformat(sep=' ', timespec='seconds')}."
    )
    await asyncio.sleep(seconds_until_due)


async def _execute_booking_attempt(
    class_name: str,
    class_date,
    job_id: int | None = None,
    window_start_time: time | None = None,
    window_end_time: time | None = None,
    seat_selection: SeatSelectionPlan | None = None,
    calendar_event_id: int | None = None,
    capture_network: bool = False,
    expected_run_at: datetime | None = None,
    dry_run: bool = False,
) -> tuple[BookingStatus, BookingResult]:
    settings = get_settings()
    adapter = ExampleGymAdapter()
    auth_state_path = ensure_auth_state_directory()

    network_capture = BookingNetworkCapture() if capture_network else None

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context(**auth_state_context_kwargs())
        try:
            await adapter.ensure_logged_in(context)
            if network_capture is not None:
                network_capture.attach(context)
            await _wait_until_booking_due(expected_run_at)
            if job_id is not None and not _booking_job_is_runnable(job_id):
                result = BookingResult(
                    status=BookingStatus.CANCELED.value,
                    message="Booking job was no longer runnable when its scheduled time arrived.",
                )
            else:
                result = await adapter.book_class(
                    context=context,
                    class_name=class_name,
                    class_date=class_date,
                    window_start_time=window_start_time,
                    window_end_time=window_end_time,
                    seat_labels_to_try=(
                        seat_selection.seat_labels if seat_selection is not None else None
                    ),
                    seat_selection_source=(
                        seat_selection.source if seat_selection is not None else None
                    ),
                    require_preferred_seat=(
                        seat_selection.hard_constraint if seat_selection is not None else False
                    ),
                    select_any_available_seat=(
                        seat_selection.select_any_available_seat
                        if seat_selection is not None
                        else False
                    ),
                    allow_any_available_fallback=(
                        seat_selection.allow_any_available_fallback
                        if seat_selection is not None
                        else False
                    ),
                    calendar_event_id=calendar_event_id,
                    dry_run=dry_run,
                )
        except Exception as exc:
            result = BookingResult(
                status=BookingStatus.FAILED.value,
                message=f"Unexpected error during booking attempt: {exc}",
            )
        finally:
            if network_capture is not None:
                result.network_artifact_path = await network_capture.save(
                    job_id=job_id,
                    class_name=class_name,
                    class_date=class_date.isoformat(),
                    expected_run_at=(
                        expected_run_at.isoformat(sep=" ", timespec="seconds")
                        if expected_run_at is not None
                        else ""
                    ),
                    status=result.status,
                    message=result.message,
                )
            await context.storage_state(path=str(auth_state_path))
            await context.close()
            await browser.close()

    result_status = BookingStatus(result.status) if result.status in BookingStatus._value2member_map_ else BookingStatus.FAILED
    return result_status, result


def _build_discovered_class_source_key(result: BookingResult) -> str:
    class_date = result.matched_class_date.isoformat() if result.matched_class_date else "unknown-date"
    class_time = result.matched_class_time.isoformat(timespec="minutes") if result.matched_class_time else "unknown-time"
    summary_lines = [
        line.strip()
        for line in result.matched_class_summary.splitlines()
        if line.strip() and not re.fullmatch(r"book now(?:\s*\(full\))?", line.strip(), re.IGNORECASE)
    ]
    summary_key = re.sub(r"\s+", " ", " ".join(summary_lines)).strip().lower()
    return f"{class_date}|{class_time}|{summary_key}"


def _build_previous_discovered_class_source_key(result: BookingResult) -> str:
    class_date = result.matched_class_date.isoformat() if result.matched_class_date else "unknown-date"
    class_time = result.matched_class_time.isoformat(timespec="minutes") if result.matched_class_time else "unknown-time"
    summary_key = re.sub(r"\s+", " ", result.matched_class_summary).strip().lower()
    return f"{class_date}|{class_time}|{summary_key}"


def _parse_discovered_class_summary(summary: str) -> dict:
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    duration_minutes = None
    if len(lines) >= 2:
        duration_match = re.search(r"(\d+)\s*min", lines[1], re.IGNORECASE)
        if duration_match:
            duration_minutes = int(duration_match.group(1))

    return {
        "title": lines[2] if len(lines) >= 3 else (lines[0] if lines else ""),
        "instructor_name": lines[3] if len(lines) >= 4 else "",
        "category": lines[4] if len(lines) >= 5 else "",
        "duration_minutes": duration_minutes,
    }


def _time_in_booking_window(candidate: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= candidate <= end
    return candidate >= start or candidate <= end


def _booking_result_target_mismatch(
    result: BookingResult,
    *,
    class_date,
    window_start_time: time | None,
    window_end_time: time | None,
    calendar_event_id: int | None = None,
) -> str:
    if (
        calendar_event_id is not None
        and result.matched_calendar_event_id is not None
        and result.matched_calendar_event_id != calendar_event_id
    ):
        return (
            f"matched calendar event {result.matched_calendar_event_id} instead of "
            f"{calendar_event_id}"
        )

    if result.matched_class_date is not None and result.matched_class_date != class_date:
        return (
            f"matched {result.matched_class_date.isoformat()} instead of "
            f"{class_date.isoformat()}"
        )

    if (
        result.matched_class_time is not None
        and window_start_time is not None
        and window_end_time is not None
        and not _time_in_booking_window(result.matched_class_time, window_start_time, window_end_time)
    ):
        return (
            f"matched {result.matched_class_time.isoformat(timespec='minutes')} outside "
            f"target window {window_start_time.isoformat(timespec='minutes')}-"
            f"{window_end_time.isoformat(timespec='minutes')}"
        )

    return ""


def _upsert_discovered_class(
    session,
    result: BookingResult,
    observed_at: datetime,
    existing_discovered_class_id: int | None = None,
    calendar_event_id: int | None = None,
) -> DiscoveredClass | None:
    if not result.matched_class_summary:
        return None

    source_key = _build_discovered_class_source_key(result)
    parsed_summary = _parse_discovered_class_summary(result.matched_class_summary)
    discovered_class = None
    if existing_discovered_class_id is not None:
        discovered_class = session.get(DiscoveredClass, existing_discovered_class_id)
    if discovered_class is None and calendar_event_id is not None:
        discovered_class = session.exec(
            select(DiscoveredClass).where(DiscoveredClass.calendar_event_id == calendar_event_id)
        ).first()
    if discovered_class is None:
        discovered_class = session.exec(
            select(DiscoveredClass).where(DiscoveredClass.source_key == source_key)
        ).first()
    if discovered_class is None:
        previous_source_key = _build_previous_discovered_class_source_key(result)
        discovered_class = session.exec(
            select(DiscoveredClass).where(DiscoveredClass.source_key == previous_source_key)
        ).first()
        if discovered_class is not None:
            discovered_class.source_key = source_key

    if discovered_class is None:
        discovered_class = DiscoveredClass(
            source_key=source_key,
            title=result.matched_class_title or parsed_summary["title"] or "Unknown class",
            class_date=result.matched_class_date or observed_at.date(),
            start_time=result.matched_class_time,
            instructor_name=parsed_summary["instructor_name"],
            category=parsed_summary["category"],
            duration_minutes=parsed_summary["duration_minutes"],
            raw_summary=result.matched_class_summary,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
    else:
        discovered_class.title = result.matched_class_title or parsed_summary["title"] or discovered_class.title
        discovered_class.class_date = result.matched_class_date or discovered_class.class_date
        discovered_class.start_time = result.matched_class_time or discovered_class.start_time
        discovered_class.instructor_name = (
            parsed_summary["instructor_name"] or discovered_class.instructor_name
        )
        discovered_class.category = parsed_summary["category"] or discovered_class.category
        discovered_class.duration_minutes = (
            parsed_summary["duration_minutes"] or discovered_class.duration_minutes
        )
        discovered_class.raw_summary = result.matched_class_summary
        discovered_class.last_seen_at = observed_at
    if calendar_event_id is not None:
        discovered_class.calendar_event_id = calendar_event_id

    session.add(discovered_class)
    session.flush()
    return discovered_class


def _calculate_retry_at(
    available_at_hint: datetime | None,
    available_at_hint_exact: bool,
    overdue_retry_count: int = 0,
) -> datetime | None:
    settings = get_settings()
    now = local_now()
    buffer = timedelta(seconds=settings.retry_after_open_seconds)

    if available_at_hint is not None:
        if available_at_hint_exact:
            if available_at_hint > now:
                return max(available_at_hint + buffer, now + timedelta(seconds=1))

            if overdue_retry_count < len(NOT_YET_OPEN_RETRY_DELAYS_SECONDS):
                return now + timedelta(seconds=NOT_YET_OPEN_RETRY_DELAYS_SECONDS[overdue_retry_count])

            return None

        fallback_time = available_at_hint.replace(
            hour=settings.classes_open_hour,
            minute=settings.classes_open_minute,
            second=0,
            microsecond=0,
        )
        return max(fallback_time + buffer, now + timedelta(minutes=1))

    return now + timedelta(minutes=settings.not_yet_open_fallback_minutes)


def _count_overdue_not_yet_open_events(session, job_id: int) -> int:
    events = session.exec(
        select(BookingEvent).where(
            BookingEvent.job_id == job_id,
            BookingEvent.status == BookingStatus.NOT_YET_OPEN,
        )
    ).all()
    overdue_count = 0
    for event in events:
        if event.site_booking_opens_at is None:
            continue
        if event.observed_at >= event.site_booking_opens_at:
            overdue_count += 1
    return overdue_count


def _should_send_notification(status: BookingStatus) -> bool:
    return status != BookingStatus.NOT_YET_OPEN


def _format_notification_subject(
    *,
    class_name: str,
    status: str,
    class_date,
    class_time: time | None,
) -> str:
    date_label = class_date.strftime("%a %b %d")
    if class_time is None:
        when_label = date_label
    else:
        time_label = class_time.strftime("%I:%M %p").lstrip("0")
        when_label = f"{date_label} at {time_label}"

    templates = {
        "booked": "BookGym: {class_name} booked for {when_label}",
        "waitlisted": "BookGym: {class_name} waitlisted for {when_label}",
        "failed": "BookGym: Booking failed for {class_name} on {when_label}",
        "dry_run": "BookGym: Dry run for {class_name} on {when_label}",
        "not_yet_open": "BookGym: {class_name} not open yet for {when_label}",
        "needs_confirmation": "BookGym: Confirmation needed for {class_name} on {when_label}",
        "pending": "BookGym: Booking pending for {class_name} on {when_label}",
        "canceled": "BookGym: {class_name} canceled for {when_label}",
    }
    template = templates.get(status, "BookGym: Booking update for {class_name} on {when_label}")
    return template.format(class_name=class_name, when_label=when_label)


async def _run_booking_attempt(attempt_id: int) -> None:
    await _run_booking_job(attempt_id)


async def _run_booking_job(job_id: int) -> None:
    settings = get_settings()
    capture_claimed_for_next_booking = False

    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None:
            return
        if cancel_job_if_paused(session, job) is not None:
            session.commit()
            return

        class_name = job.target_match_text
        class_date = job.class_date
        window_start_time = job.window_start_time
        window_end_time = job.window_end_time
        existing_discovered_class_id = job.discovered_class_id
        calendar_event_id = job.calendar_event_id
        expected_run_at = job.expected_run_at
        is_dry_run_job = job.rule_name == DRY_RUN_SELECTION_RULE_NAME
        if not is_dry_run_job:
            capture_claimed_for_next_booking = claim_next_booking_capture(job_id)
        capture_network = job.capture_network or capture_claimed_for_next_booking
        if capture_claimed_for_next_booking:
            print(f"One-shot network capture claimed by live booking job {job_id}.")
        discovered_class = (
            session.get(DiscoveredClass, existing_discovered_class_id)
            if existing_discovered_class_id is not None
            else None
        )
        seat_selection = resolve_booking_seat_selection(
            session=session,
            discovered_class=discovered_class,
            user_id=job.user_id,
        )
        print(
            f"Seat selection for booking job {job_id}: "
            f"source={seat_selection.source!r}, "
            f"seats={seat_selection.seat_labels}, "
            f"hard_constraint={seat_selection.hard_constraint}, "
            f"select_any_available={seat_selection.select_any_available_seat}, "
            f"allow_fallback={seat_selection.allow_any_available_fallback}"
        )

    result_status, result = await _execute_booking_attempt(
        class_name=class_name,
        class_date=class_date,
        job_id=job_id,
        window_start_time=window_start_time,
        window_end_time=window_end_time,
        seat_selection=seat_selection,
        calendar_event_id=calendar_event_id,
        capture_network=capture_network,
        expected_run_at=expected_run_at,
        dry_run=is_dry_run_job,
    )

    with get_session() as session:
        job = session.get(BookingJob, job_id)
        if job is None:
            return

        observed_at = local_now()
        target_mismatch = _booking_result_target_mismatch(
            result,
            class_date=class_date,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            calendar_event_id=calendar_event_id,
        )
        discovered_class = None
        if target_mismatch:
            if result_status in [BookingStatus.BOOKED, BookingStatus.WAITLISTED]:
                result_status = BookingStatus.NEEDS_CONFIRMATION
            job_message = (
                f"{result.message} Target mismatch: {target_mismatch}. "
                "BookGym did not update the intended class record."
            )
        else:
            discovered_class = _upsert_discovered_class(
                session=session,
                result=result,
                observed_at=observed_at,
                existing_discovered_class_id=existing_discovered_class_id,
                calendar_event_id=result.matched_calendar_event_id or calendar_event_id,
            )
            job_message = result.message

        if result.network_artifact_path:
            job_message = f"{job_message} Network capture: {result.network_artifact_path}"

        job.status = result_status
        job.last_message = job_message
        job.last_screenshot_path = result.screenshot_path
        if capture_network:
            job.capture_network = False
        if result.network_artifact_path:
            job.last_network_artifact_path = result.network_artifact_path
        job.updated_at = observed_at
        if discovered_class is not None:
            job.discovered_class_id = discovered_class.id
            job.calendar_event_id = discovered_class.calendar_event_id or job.calendar_event_id
        if result.available_at_hint is not None:
            job.site_booking_opens_at = result.available_at_hint
        if result_status == BookingStatus.NOT_YET_OPEN:
            overdue_retry_count = _count_overdue_not_yet_open_events(session=session, job_id=job.id)
            retry_at = _calculate_retry_at(
                result.available_at_hint,
                result.available_at_hint_exact,
                overdue_retry_count=overdue_retry_count,
            )
            if retry_at is None:
                result_status = BookingStatus.FAILED
                job.status = result_status
                job.last_message = (
                    f"{result.message} BookGym retried after the listed opening time "
                    "(15s, 1m, 5m backoff), but the site still reported the class as not yet open."
                )
            else:
                job.expected_run_at = retry_at
                job.last_message = (
                    f"{result.message} Retry scheduled for {retry_at.isoformat(sep=' ', timespec='seconds')}."
                )

        event = BookingEvent(
            job_id=job.id,
            discovered_class_id=job.discovered_class_id,
            event_type="booking_run",
            status=result_status,
            message=job.last_message,
            screenshot_path=result.screenshot_path,
            network_artifact_path=result.network_artifact_path,
            site_booking_opens_at=result.available_at_hint,
            observed_at=observed_at,
        )
        session.add(job)
        session.add(event)
        session.commit()
        target_class_name = job.target_match_text
        final_status = job.status.value
        final_message = job.last_message
        final_class_date = job.class_date
        final_class_time = job.window_start_time
        final_seat_label = result.selected_seat_label
        final_screenshot_path = job.last_screenshot_path
        final_email_recipients = (
            parse_recipient_csv(job.notification_email_override)
            if job.notification_email_override is not None
            else None
        )
        final_sms_recipients = (
            parse_recipient_csv(job.notification_sms_override)
            if job.notification_sms_override is not None
            else None
        )

    if (
        capture_claimed_for_next_booking
        and result.network_artifact_path
        and BookingStatus(final_status)
        in [
            BookingStatus.BOOKED,
            BookingStatus.WAITLISTED,
            BookingStatus.NEEDS_CONFIRMATION,
        ]
    ):
        complete_next_booking_capture(job_id)
        print(f"One-shot network capture completed by live booking job {job_id}.")

    if _should_send_notification(BookingStatus(final_status)):
        send_notification(
            subject=_format_notification_subject(
                class_name=target_class_name,
                status=final_status,
                class_date=final_class_date,
                class_time=final_class_time,
            ),
            body=final_message,
            context=NotificationContext(
                class_name=target_class_name,
                status=final_status,
                class_date=final_class_date,
                class_time=final_class_time,
                seat_label=final_seat_label,
                is_dry_run=is_dry_run_job,
                screenshot_path=final_screenshot_path,
            ),
            email_recipients=final_email_recipients,
            sms_recipients=final_sms_recipients,
        )


async def _run_manual_booking_attempt(class_name: str, class_date) -> None:
    result_status, result = await _execute_booking_attempt(
        class_name=class_name,
        class_date=class_date,
    )
    print(f"Status: {result_status.value}")
    print(f"Message: {result.message}")
    if result.screenshot_path:
        print(f"Screenshot: {result.screenshot_path}")


def book_next_available_class() -> dict[str, object]:
    init_db()
    settings = get_settings()
    now = local_now()
    window_end = now + timedelta(seconds=settings.booking_worker_interval_seconds + 10)

    with get_session() as session:
        queued_jobs = session.exec(
            select(BookingJob).where(
                BookingJob.rule_name.in_([MANUAL_SELECTION_RULE_NAME, DRY_RUN_SELECTION_RULE_NAME]),
                BookingJob.status.in_([BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN]),
                BookingJob.expected_run_at <= window_end,
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

    if next_job is None:
        return {"action": "idle", "message": "No due booking jobs were ready."}

    seconds_until_due = max(0.0, (next_job.expected_run_at - now).total_seconds())
    if seconds_until_due > 0:
        print(
            f"Prewarming browser for booking job {next_job.id} {seconds_until_due:.1f}s before "
            f"{next_job.expected_run_at.isoformat(sep=' ', timespec='seconds')}."
        )

    action_label = "Preparing" if seconds_until_due > 0 else "Running"
    print(
        f"{action_label} selected booking job "
        f"{next_job.id} "
        f"on {next_job.class_date.isoformat()} "
        f"(expected at {next_job.expected_run_at.isoformat(sep=' ', timespec='seconds')})"
    )
    asyncio.run(_run_booking_job(next_job.id))
    return {
        "action": "ran_job",
        "job_id": next_job.id,
        "class_date": next_job.class_date.isoformat(),
        "expected_run_at": next_job.expected_run_at.isoformat(sep=" ", timespec="seconds"),
    }
