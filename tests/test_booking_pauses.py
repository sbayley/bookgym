from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, create_engine, select

from app import db as app_db
from app.booking_pauses import clear_booking_pause, set_booking_pause
from app.job_sources import MANUAL_SELECTION_RULE_NAME
from app.main import select_discovered_class
from app.jobs.book_class import _run_booking_job
from app.models import (
    ApprovalStatus,
    BookingApproval,
    BookingJob,
    BookingPause,
    BookingStatus,
    DiscoveredClass,
    WeeklyScheduleItem,
)
from app.weekly_schedule import materialize_weekly_schedule, weekly_schedule_scope_key


class BookingPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()
        self.now = datetime(2026, 8, 9, 12, 0)

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()

    def _create_class_and_job(
        self,
        *,
        class_date: date,
        status: BookingStatus = BookingStatus.PENDING,
        user_id: int | None = None,
    ) -> tuple[int, int]:
        with Session(app_db.engine) as session:
            discovered = DiscoveredClass(
                source_key=f"pause:{user_id}:{class_date.isoformat()}:{status.value}",
                calendar_event_id=1000 + class_date.day,
                title="Yoga Level 1",
                class_date=class_date,
                start_time=time(10, 30),
                instructor_name="Coach",
                category="Yoga",
                raw_summary="Yoga Level 1\nBook Now",
                first_seen_at=self.now,
                last_seen_at=self.now,
            )
            session.add(discovered)
            session.flush()
            job = BookingJob(
                user_id=user_id,
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered.title,
                class_date=class_date,
                window_start_time=discovered.start_time,
                window_end_time=discovered.start_time,
                expected_run_at=self.now + timedelta(hours=1),
                calendar_event_id=discovered.calendar_event_id,
                discovered_class_id=discovered.id,
                status=status,
                capture_network=True,
                created_at=self.now,
                updated_at=self.now,
            )
            session.add(job)
            session.commit()
            return int(discovered.id), int(job.id)

    def test_pause_cancels_only_runnable_jobs_and_expires_approval(self) -> None:
        _class_id, pending_job_id = self._create_class_and_job(
            class_date=date(2026, 8, 15),
        )
        _booked_class_id, booked_job_id = self._create_class_and_job(
            class_date=date(2026, 8, 16),
            status=BookingStatus.BOOKED,
        )
        with Session(app_db.engine) as session:
            session.add(
                BookingApproval(
                    job_id=pending_job_id,
                    token="PAUSE123",
                    status=ApprovalStatus.PENDING,
                    request_due_at=self.now,
                    response_cutoff_at=self.now + timedelta(minutes=30),
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.commit()

        with patch("app.booking_pauses.local_today", return_value=date(2026, 8, 9)), patch(
            "app.booking_pauses.local_now", return_value=self.now
        ), patch("app.weekly_schedule.local_today", return_value=date(2026, 8, 9)):
            payload = set_booking_pause(
                start_date=date(2026, 8, 14),
                resume_date=date(2026, 8, 18),
            )

        self.assertEqual([pending_job_id], payload["canceled_job_ids"])
        self.assertEqual("2026-08-18", payload["pause"]["resume_date"])
        with Session(app_db.engine) as session:
            pending_job = session.get(BookingJob, pending_job_id)
            booked_job = session.get(BookingJob, booked_job_id)
            approval = session.exec(
                select(BookingApproval).where(BookingApproval.job_id == pending_job_id)
            ).one()
        self.assertEqual(BookingStatus.CANCELED, pending_job.status)
        self.assertFalse(pending_job.capture_network)
        self.assertIn("bookings resume on 2026-08-18", pending_job.last_message)
        self.assertEqual(BookingStatus.BOOKED, booked_job.status)
        self.assertEqual(ApprovalStatus.EXPIRED, approval.status)

    def test_pause_blocks_selection_for_class_date(self) -> None:
        class_id, _job_id = self._create_class_and_job(
            class_date=date(2026, 8, 15),
            status=BookingStatus.CANCELED,
        )
        with Session(app_db.engine) as session:
            session.add(
                BookingPause(
                    start_date=date(2026, 8, 14),
                    resume_date=date(2026, 8, 18),
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.commit()

        with self.assertRaisesRegex(Exception, "Bookings are paused for this class date"):
            select_discovered_class(class_id)

    def test_clearing_pause_restores_only_recurring_job(self) -> None:
        class_id, job_id = self._create_class_and_job(
            class_date=date(2026, 8, 15),
        )
        with Session(app_db.engine) as session:
            session.add(
                WeeklyScheduleItem(
                    scope_key=weekly_schedule_scope_key(
                        weekday=6,
                        start_time=time(10, 30),
                        title="Yoga Level 1",
                    ),
                    weekday=6,
                    start_time=time(10, 30),
                    title="Yoga Level 1",
                    title_key="yoga-level-1",
                    category="Yoga",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.commit()

        with patch("app.booking_pauses.local_today", return_value=date(2026, 8, 9)), patch(
            "app.booking_pauses.local_now", return_value=self.now
        ), patch("app.weekly_schedule.local_today", return_value=date(2026, 8, 9)):
            set_booking_pause(
                start_date=date(2026, 8, 14),
                resume_date=date(2026, 8, 18),
            )
            paused = materialize_weekly_schedule()
            cleared = clear_booking_pause()

        self.assertEqual(1, paused["paused_count"])
        self.assertTrue(cleared["removed"])
        with Session(app_db.engine) as session:
            job = session.get(BookingJob, job_id)
            pauses = list(session.exec(select(BookingPause)).all())
        self.assertEqual(BookingStatus.PENDING, job.status)
        self.assertEqual("Selected from weekly schedule", job.last_message)
        self.assertEqual([], pauses)
        self.assertEqual(class_id, job.discovered_class_id)

    def test_pause_is_scoped_to_owning_user(self) -> None:
        _class_id, first_job_id = self._create_class_and_job(
            class_date=date(2026, 8, 15),
            user_id=1,
        )
        _other_class_id, second_job_id = self._create_class_and_job(
            class_date=date(2026, 8, 16),
            user_id=2,
        )
        with patch("app.booking_pauses.local_today", return_value=date(2026, 8, 9)), patch(
            "app.booking_pauses.local_now", return_value=self.now
        ), patch("app.weekly_schedule.local_today", return_value=date(2026, 8, 9)):
            set_booking_pause(
                start_date=date(2026, 8, 14),
                resume_date=date(2026, 8, 18),
                user_id=1,
            )

        with Session(app_db.engine) as session:
            first_job = session.get(BookingJob, first_job_id)
            second_job = session.get(BookingJob, second_job_id)
        self.assertEqual(BookingStatus.CANCELED, first_job.status)
        self.assertEqual(BookingStatus.PENDING, second_job.status)

    def test_worker_rechecks_pause_before_opening_browser(self) -> None:
        _class_id, job_id = self._create_class_and_job(
            class_date=date(2026, 8, 15),
        )
        with Session(app_db.engine) as session:
            session.add(
                BookingPause(
                    start_date=date(2026, 8, 14),
                    resume_date=date(2026, 8, 18),
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.commit()

        execute_attempt = AsyncMock()
        with patch("app.jobs.book_class._execute_booking_attempt", execute_attempt):
            asyncio.run(_run_booking_job(job_id))

        execute_attempt.assert_not_awaited()
        with Session(app_db.engine) as session:
            job = session.get(BookingJob, job_id)
        self.assertEqual(BookingStatus.CANCELED, job.status)
        self.assertIn("bookings resume on 2026-08-18", job.last_message)


if __name__ == "__main__":
    unittest.main()
