from __future__ import annotations

import asyncio
from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, create_engine

from app import db as app_db
from app.job_sources import DRY_RUN_SELECTION_RULE_NAME, MANUAL_SELECTION_RULE_NAME
from app.adapters.base import BookingResult
from app.jobs.book_class import _run_booking_job, _wait_until_booking_due, book_next_available_class
from app.models import BookingJob, BookingStatus, DiscoveredClass
from app.scheduler import get_next_due_job
from app.time_utils import local_now
from scripts.schedule_dry_run_job import schedule_dry_run_job


class TempDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.capture_marker_patch = patch(
            "app.network_capture.CAPTURE_NEXT_BOOKING_MARKER",
            Path(self.temp_dir.name) / "capture-next-booking.json",
        )
        self.capture_marker_patch.start()
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        self.capture_marker_patch.stop()
        self.temp_dir.cleanup()


class DryRunJobTests(TempDatabaseTestCase):
    def create_discovered_class(self, *, title: str = "Yoga Level 1") -> DiscoveredClass:
        discovered_class = DiscoveredClass(
            source_key=f"calendar_event:{title}",
            calendar_event_id=456,
            title=title,
            class_date=date(2026, 7, 5),
            start_time=time(8, 15),
            instructor_name="Coach",
            category="Yoga",
            duration_minutes=60,
            is_full=False,
            is_booked_at_gym=False,
            can_book_event=True,
            raw_summary="08:15 AM\n(60 min)\nYoga Level 1\nCoach\nYoga\nBook Now",
            first_seen_at=local_now(),
            last_seen_at=local_now(),
        )
        with Session(app_db.engine) as session:
            session.add(discovered_class)
            session.commit()
            session.refresh(discovered_class)
        return discovered_class

    def test_schedule_dry_run_job_creates_pending_job(self) -> None:
        discovered_class = self.create_discovered_class()

        job = schedule_dry_run_job(discovered_class=discovered_class, minutes=15)

        self.assertEqual(DRY_RUN_SELECTION_RULE_NAME, job.rule_name)
        self.assertEqual(BookingStatus.PENDING, job.status)
        self.assertEqual(discovered_class.id, job.discovered_class_id)
        self.assertGreaterEqual(job.expected_run_at, local_now() + timedelta(minutes=14))
        self.assertIsNone(job.notification_email_override)

    def test_schedule_dry_run_job_persists_email_override(self) -> None:
        discovered_class = self.create_discovered_class()

        job = schedule_dry_run_job(
            discovered_class=discovered_class,
            minutes=15,
            notification_email_recipients=["one@example.com", "two@example.com", "one@example.com"],
        )

        self.assertEqual("one@example.com,two@example.com", job.notification_email_override)

    def test_scheduler_next_due_includes_dry_run_jobs(self) -> None:
        discovered_class = self.create_discovered_class()
        dry_run_job = schedule_dry_run_job(discovered_class=discovered_class, minutes=10)

        with Session(app_db.engine) as session:
            live_job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=discovered_class.class_date,
                window_start_time=discovered_class.start_time,
                window_end_time=discovered_class.start_time,
                expected_run_at=local_now() + timedelta(minutes=20),
                calendar_event_id=discovered_class.calendar_event_id,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                last_message="Selected from admin calendar",
                created_at=local_now(),
                updated_at=local_now(),
            )
            session.add(live_job)
            session.commit()

        next_job = get_next_due_job()

        self.assertIsNotNone(next_job)
        assert next_job is not None
        self.assertEqual(dry_run_job.id, next_job.id)


    def test_booking_worker_does_not_rewrite_job_class_when_result_matches_wrong_slot(self) -> None:
        discovered_class = self.create_discovered_class()
        now = local_now()
        discovered_class_id = discovered_class.id
        assert discovered_class_id is not None

        with Session(app_db.engine) as session:
            job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=date(2026, 7, 13),
                window_start_time=time(11, 45),
                window_end_time=time(11, 45),
                expected_run_at=now,
                calendar_event_id=discovered_class.calendar_event_id,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                last_message="Selected from admin calendar",
                created_at=now,
                updated_at=now,
            )
            discovered_class.class_date = date(2026, 7, 13)
            discovered_class.start_time = time(11, 45)
            discovered_class.instructor_name = "Nikki Pool"
            session.add(discovered_class)
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        assert job_id is not None
        wrong_result = BookingResult(
            status=BookingStatus.BOOKED.value,
            message="Booked the visible Yoga Level 1 card.",
            matched_class_title="Yoga Level 1",
            matched_class_date=date(2026, 7, 12),
            matched_class_time=time(8, 15),
            matched_class_summary='08:15 AM\n(60 min)\nYoga Level 1\nHolly Jackson Sigman\nYoga\nBook Now',
        )

        with (
            patch(
                "app.jobs.book_class._execute_booking_attempt",
                AsyncMock(return_value=(BookingStatus.BOOKED, wrong_result)),
            ),
            patch("app.jobs.book_class.send_notification"),
        ):
            asyncio.run(_run_booking_job(job_id))

        with Session(app_db.engine) as session:
            job = session.get(BookingJob, job_id)
            refreshed_class = session.get(DiscoveredClass, discovered_class_id)

        assert job is not None
        assert refreshed_class is not None
        self.assertEqual(BookingStatus.NEEDS_CONFIRMATION, job.status)
        self.assertIn("Target mismatch", job.last_message)
        self.assertEqual(discovered_class_id, job.discovered_class_id)
        self.assertEqual(date(2026, 7, 13), refreshed_class.class_date)
        self.assertEqual(time(11, 45), refreshed_class.start_time)
        self.assertEqual("Nikki Pool", refreshed_class.instructor_name)

    def test_booking_worker_does_not_rewrite_job_class_when_result_matches_wrong_event_id(self) -> None:
        discovered_class = self.create_discovered_class()
        now = local_now()
        discovered_class_id = discovered_class.id
        assert discovered_class_id is not None

        target_class_date = discovered_class.class_date
        target_start_time = discovered_class.start_time
        target_calendar_event_id = discovered_class.calendar_event_id

        with Session(app_db.engine) as session:
            job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=target_class_date,
                window_start_time=target_start_time,
                window_end_time=target_start_time,
                expected_run_at=now,
                calendar_event_id=target_calendar_event_id,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                last_message="Selected from admin calendar",
                created_at=now,
                updated_at=now,
            )
            discovered_class.instructor_name = "Original Coach"
            session.add(discovered_class)
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.id

        assert job_id is not None
        wrong_result = BookingResult(
            status=BookingStatus.BOOKED.value,
            message="Booked the visible Yoga Level 1 card.",
            matched_class_title="Yoga Level 1",
            matched_class_date=target_class_date,
            matched_class_time=target_start_time,
            matched_class_summary='08:15 AM\n(60 min)\nYoga Level 1\nDifferent Coach\nYoga\nBook Now',
            matched_calendar_event_id=999,
        )

        with (
            patch(
                "app.jobs.book_class._execute_booking_attempt",
                AsyncMock(return_value=(BookingStatus.BOOKED, wrong_result)),
            ),
            patch("app.jobs.book_class.send_notification"),
        ):
            asyncio.run(_run_booking_job(job_id))

        with Session(app_db.engine) as session:
            job = session.get(BookingJob, job_id)
            refreshed_class = session.get(DiscoveredClass, discovered_class_id)

        assert job is not None
        assert refreshed_class is not None
        self.assertEqual(BookingStatus.NEEDS_CONFIRMATION, job.status)
        self.assertIn("matched calendar event 999 instead of 456", job.last_message)
        self.assertEqual(discovered_class_id, job.discovered_class_id)
        self.assertEqual(456, refreshed_class.calendar_event_id)
        self.assertEqual("Original Coach", refreshed_class.instructor_name)

    def test_already_booked_result_sends_booked_email_with_clear_details(self) -> None:
        discovered_class = self.create_discovered_class(title="BodyBalance")
        now = local_now()
        with Session(app_db.engine) as session:
            job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=discovered_class.class_date,
                window_start_time=discovered_class.start_time,
                window_end_time=discovered_class.start_time,
                expected_run_at=now,
                calendar_event_id=discovered_class.calendar_event_id,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = int(job.id)

        message = (
            "BodyBalance was already booked at the gym; the class details showed "
            "'Cancel Booking'. No additional booking was attempted."
        )
        result = BookingResult(
            status=BookingStatus.BOOKED.value,
            message=message,
            matched_class_title=discovered_class.title,
            matched_class_date=discovered_class.class_date,
            matched_class_time=discovered_class.start_time,
            matched_calendar_event_id=discovered_class.calendar_event_id,
        )

        with (
            patch(
                "app.jobs.book_class._execute_booking_attempt",
                AsyncMock(return_value=(BookingStatus.BOOKED, result)),
            ),
            patch("app.jobs.book_class.send_notification") as notification_mock,
        ):
            asyncio.run(_run_booking_job(job_id))

        notification_mock.assert_called_once()
        notification = notification_mock.call_args.kwargs
        self.assertIn("booked for", notification["subject"])
        self.assertNotIn("failed", notification["subject"].lower())
        self.assertEqual(message, notification["body"])
        self.assertEqual("booked", notification["context"].status)

    def test_booking_worker_starts_browser_warmup_before_job_is_due(self) -> None:
        discovered_class = self.create_discovered_class()
        now = local_now()
        due_at = now + timedelta(seconds=12)

        with Session(app_db.engine) as session:
            job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=discovered_class.class_date,
                window_start_time=discovered_class.start_time,
                window_end_time=discovered_class.start_time,
                expected_run_at=due_at,
                calendar_event_id=discovered_class.calendar_event_id,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                last_message="Selected from admin calendar",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            session.refresh(job)

        fake_settings = SimpleNamespace(booking_worker_interval_seconds=20)

        with (
            patch("app.jobs.book_class.get_settings", return_value=fake_settings),
            patch("app.jobs.book_class.local_now", return_value=now),
            patch("app.jobs.book_class.asyncio.run", side_effect=lambda coro: coro.close()) as run_mock,
        ):
            result = book_next_available_class()

        run_mock.assert_called_once()
        self.assertEqual("ran_job", result["action"])
        self.assertEqual(job.id, result["job_id"])

    def test_browser_warmup_waits_only_for_remaining_due_time(self) -> None:
        now = local_now()
        due_at = now + timedelta(seconds=12)
        sleep_mock = AsyncMock()

        with (
            patch("app.jobs.book_class.local_now", return_value=now),
            patch("app.jobs.book_class.asyncio.sleep", sleep_mock),
        ):
            asyncio.run(_wait_until_booking_due(due_at))

        sleep_mock.assert_awaited_once()
        self.assertAlmostEqual(12, sleep_mock.await_args.args[0], delta=0.01)


if __name__ == "__main__":
    unittest.main()
