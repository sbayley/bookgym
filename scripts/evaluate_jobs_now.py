from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import sys

from playwright.async_api import async_playwright
from sqlmodel import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.gym_site import ExampleGymAdapter
from app.adapters.base import BookingResult
from app.db import get_session, init_db
from app.config import get_settings
from app.jobs.book_class import _upsert_discovered_class
from app.job_sources import MANUAL_SELECTION_RULE_NAME
from app.models import BookingJob, BookingStatus, DiscoveryEvent
from app.time_utils import local_now


def visible_week_bounds(visible_dates: set) -> tuple | tuple[None, None]:
    if not visible_dates:
        return None, None
    sorted_dates = sorted(visible_dates)
    return sorted_dates[0], sorted_dates[-1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Complete the booking instead of stopping after seat selection.",
    )
    parser.add_argument(
        "--probe-booking-flow",
        action="store_true",
        help="Click into the booking flow to detect booking gates or seat selection without clicking Done.",
    )
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="Also evaluate jobs that are already marked failed.",
    )
    parser.add_argument(
        "--job-id",
        action="append",
        type=int,
        default=[],
        help="Evaluate only this job ID. Can be provided more than once.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    init_db()
    settings = get_settings()

    included_statuses = [BookingStatus.PENDING, BookingStatus.NOT_YET_OPEN]
    if args.include_failed:
        included_statuses.append(BookingStatus.FAILED)

    with get_session() as session:
        statement = select(BookingJob).order_by(BookingJob.expected_run_at, BookingJob.id)
        if args.job_id:
            statement = statement.where(BookingJob.id.in_(args.job_id))
        else:
            statement = statement.where(BookingJob.status.in_(included_statuses))
            statement = statement.where(BookingJob.rule_name == MANUAL_SELECTION_RULE_NAME)
        jobs = list(session.exec(statement).all())

    if args.job_id:
        found_ids = {job.id for job in jobs}
        missing_ids = sorted(set(args.job_id) - {job_id for job_id in found_ids if job_id is not None})
        for missing_id in missing_ids:
            print(f"Job {missing_id} not found.")

    if args.job_id and not args.include_failed:
        jobs = [job for job in jobs if job.status in included_statuses]
        skipped_ids = sorted(
            job_id
            for job_id in args.job_id
            if job_id not in {job.id for job in jobs}
        )
        for skipped_id in skipped_ids:
            if skipped_id not in missing_ids:
                print(f"Job {skipped_id} is not pending/not_yet_open. Use --include-failed if needed.")

    if not jobs:
        print("No matching selected booking jobs found to evaluate.")
        return

    auth_state_path = Path(settings.auth_state_path)
    if not auth_state_path.exists():
        print(f"Saved auth state not found at {auth_state_path}. Run python scripts/login_once.py first.")
        return

    adapter = ExampleGymAdapter()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context(storage_state=str(auth_state_path))
        page = None
        visible_dates = set()
        try:
            await adapter.ensure_logged_in(context)
            for job in jobs:
                print(
                    f"Evaluating job {job.id}: source='manual_selection', "
                    f"class_date={job.class_date.isoformat()}, "
                    f"stored_status={job.status.value}, "
                    f"expected_run_at={job.expected_run_at.isoformat(sep=' ', timespec='seconds')}"
                )

                if page is None or job.class_date not in visible_dates:
                    if page is not None and not page.is_closed():
                        await page.close()
                    page, headers = await adapter.open_calendar_week_containing(
                        context=context,
                        target_date=job.class_date,
                    )
                    visible_dates = {header.slot_date for header in headers}
                    if visible_dates:
                        print(
                            "Loaded week dates: "
                            + ", ".join(sorted(day.isoformat() for day in visible_dates))
                        )
                    else:
                        print("Loaded week dates: unavailable")

                if visible_dates and job.class_date not in visible_dates:
                    print("Observed status: skipped")
                    print("Observed message: Could not navigate to a calendar week containing this job date.")
                    observed_at = local_now()
                    visible_week_start, visible_week_end = visible_week_bounds(visible_dates)
                    with get_session() as session:
                        session.add(
                            DiscoveryEvent(
                                job_id=job.id,
                                event_type="navigation_failed",
                                message="Could not navigate to a calendar week containing this job date.",
                                visible_week_start=visible_week_start,
                                visible_week_end=visible_week_end,
                                observed_at=observed_at,
                            )
                        )
                        session.commit()
                    print("DB job status unchanged.")
                    print()
                    continue

                try:
                    print("Searching visible calendar...", flush=True)
                    candidate = await adapter._find_booking_candidate(
                        page=page,
                        class_name=job.target_match_text,
                        class_date=job.class_date,
                        window_start_time=job.window_start_time,
                        window_end_time=job.window_end_time,
                        calendar_event_id=job.calendar_event_id,
                    )
                    if candidate is None:
                        observed_at = local_now()
                        visible_week_start, visible_week_end = visible_week_bounds(visible_dates)
                        with get_session() as session:
                            session.add(
                                DiscoveryEvent(
                                    job_id=job.id,
                                    event_type="no_match",
                                    message=(
                                        "No matching slot found for selected booking job "
                                        f"on {job.class_date.isoformat()}."
                                    ),
                                    visible_week_start=visible_week_start,
                                    visible_week_end=visible_week_end,
                                    observed_at=observed_at,
                                )
                            )
                            session.commit()
                        print("Observed status: failed")
                        print(
                            "Observed message: Could not find a calendar slot for "
                            f"selected booking job {job.id} for {job.class_date.isoformat()} "
                            f"{job.window_start_time.isoformat(timespec='minutes') if job.window_start_time else '00:00'}-"
                            f"{job.window_end_time.isoformat(timespec='minutes') if job.window_end_time else '23:59'} "
                            "on the currently loaded calendar week."
                        )
                        print("DB job status unchanged.")
                        print()
                        continue

                    print("Observed status: matched")
                    print(f"Observed class: {candidate.summary.replace(chr(10), ' | ')}")
                    discovery_result = BookingResult(
                        status="matched",
                        message="Calendar match only. Booking flow was not opened.",
                        matched_class_title=adapter._extract_candidate_title(candidate.summary),
                        matched_class_date=candidate.slot_date or job.class_date,
                        matched_class_time=candidate.slot_time,
                        matched_class_summary=candidate.summary,
                        matched_calendar_event_id=getattr(candidate, "calendar_event_id", None),
                    )
                    with get_session() as session:
                        current_job = session.get(BookingJob, job.id)
                        if current_job is not None:
                            observed_at = local_now()
                            visible_week_start, visible_week_end = visible_week_bounds(visible_dates)
                            discovered_class = _upsert_discovered_class(
                                session=session,
                                result=discovery_result,
                                observed_at=observed_at,
                                existing_discovered_class_id=current_job.discovered_class_id,
                                calendar_event_id=current_job.calendar_event_id,
                            )
                            if discovered_class is not None:
                                current_job.discovered_class_id = discovered_class.id
                                current_job.last_message = (
                                    "Matched calendar slot: "
                                    + candidate.summary.replace("\n", " | ")
                                )
                                current_job.updated_at = observed_at
                                session.add(current_job)
                                session.add(
                                    DiscoveryEvent(
                                        job_id=current_job.id,
                                        discovered_class_id=discovered_class.id,
                                        event_type="matched_class",
                                        message=(
                                            "Matched calendar slot: "
                                            + candidate.summary.replace("\n", " | ")
                                        ),
                                        visible_week_start=visible_week_start,
                                        visible_week_end=visible_week_end,
                                        observed_at=observed_at,
                                    )
                                )
                            session.commit()

                    if not args.probe_booking_flow and not args.live:
                        print("Observed message: Calendar match only. Booking flow was not opened.")
                        print("DB job status unchanged; discovery metadata updated.")
                        print()
                        continue

                    result = await adapter.book_candidate(
                        page=page,
                        candidate=candidate,
                        class_name=job.target_match_text,
                        class_date=job.class_date,
                        dry_run=not args.live,
                    )
                    result_status = (
                        BookingStatus(result.status)
                        if result.status in BookingStatus._value2member_map_
                        else BookingStatus.FAILED
                    )
                    print(f"Observed status: {result_status.value}")
                    print(f"Observed message: {result.message}")
                    if result.available_at_hint is not None:
                        precision = "exact" if result.available_at_hint_exact else "date-only"
                        print(
                            "Observed site booking hint: "
                            f"{result.available_at_hint.isoformat(sep=' ', timespec='seconds')} ({precision})"
                        )
                    if result.matched_class_summary:
                        print(f"Observed class: {result.matched_class_summary.replace(chr(10), ' | ')}")
                    if result.screenshot_path:
                        print(f"Screenshot: {result.screenshot_path}")

                    observed_at = local_now()
                    visible_week_start, visible_week_end = visible_week_bounds(visible_dates)
                    with get_session() as session:
                        current_job = session.get(BookingJob, job.id)
                        session.add(
                            DiscoveryEvent(
                                job_id=job.id,
                                discovered_class_id=current_job.discovered_class_id if current_job else None,
                                event_type=f"booking_probe_{result_status.value}",
                                message=result.message,
                                visible_week_start=visible_week_start,
                                visible_week_end=visible_week_end,
                                observed_at=observed_at,
                            )
                        )
                        session.commit()

                    print("DB job status unchanged.")
                    print()
                    if page is not None and not page.is_closed():
                        await page.close()
                    page = None
                    visible_dates = set()
                finally:
                    if (args.probe_booking_flow or args.live) and page is not None and not page.is_closed():
                        await page.close()
                        page = None
                        visible_dates = set()
        finally:
            if page is not None and not page.is_closed():
                await page.close()
            await context.storage_state(path=str(auth_state_path))
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
