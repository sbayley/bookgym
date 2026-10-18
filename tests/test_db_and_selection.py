from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session, create_engine, select

from app import db as app_db
from app.job_sources import DRY_RUN_SELECTION_RULE_NAME, MANUAL_SELECTION_RULE_NAME
from app.main import clear_week_watched_classes, select_discovered_class
from app.models import (
    BookingJob,
    BookingStatus,
    DiscoveredClass,
    WeeklyScheduleItem,
    WeeklyScheduleSkip,
)
from app.time_utils import local_now
from app.weekly_schedule import (
    materialize_weekly_schedule,
    replace_weekly_schedule_from_week,
    resume_weekly_schedule_occurrence,
    skip_weekly_schedule_occurrence,
    weekly_schedule_scope_key,
)


class TempDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()


class DatabaseMigrationTests(TempDatabaseTestCase):
    def test_init_db_backfills_booking_job_event_id_and_drops_legacy_tables(self) -> None:
        observed_at = local_now().isoformat(sep=" ")

        with app_db.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE classpreference (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE bookingattempt (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                """
                INSERT INTO discoveredclass (
                    source_key, calendar_event_id, title, class_date, start_time,
                    instructor_name, category, duration_minutes, event_type,
                    seat_title,
                    gym_booking_status, gym_booking_spot_labels, gym_booking_actions,
                    deep_link_url, booking_probe_status, booking_probe_message,
                    raw_summary, api_payload, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "calendar_event:777",
                    777,
                    "Yoga Level 1",
                    "2026-07-08",
                    "11:45:00",
                    "Coach",
                    "Yoga",
                    60,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "11:45 AM\n(60 min)\nYoga Level 1\nCoach\nYoga\nBook Now",
                    "{}",
                    observed_at,
                    observed_at,
                ),
            )
            discovered_class_id = connection.exec_driver_sql(
                "SELECT id FROM discoveredclass WHERE source_key = ?",
                ("calendar_event:777",),
            ).scalar_one()
            connection.exec_driver_sql(
                """
                INSERT INTO bookingjob (
                    rule_name, target_match_text, class_date, window_start_time, window_end_time,
                    expected_run_at, calendar_event_id, site_booking_opens_at, discovered_class_id,
                    status, last_message, last_screenshot_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Manual selection",
                    "Yoga Level 1",
                    "2026-07-08",
                    "11:45:00",
                    "11:45:00",
                    "2026-07-05 19:00:00",
                    None,
                    None,
                    discovered_class_id,
                    BookingStatus.PENDING.value,
                    "Selected from admin calendar",
                    "",
                    observed_at,
                    observed_at,
                ),
            )

        app_db.init_db()

        with app_db.engine.begin() as connection:
            calendar_event_id = connection.exec_driver_sql(
                "SELECT calendar_event_id FROM bookingjob"
            ).scalar_one()
            remaining_legacy_tables = connection.exec_driver_sql(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name IN ('classpreference', 'bookingattempt')
                """
            ).fetchall()

        self.assertEqual(777, calendar_event_id)
        self.assertEqual([], remaining_legacy_tables)


class SelectionEndpointTests(TempDatabaseTestCase):
    def test_select_route_persists_calendar_event_id_on_job(self) -> None:
        with Session(app_db.engine) as session:
            discovered_class = DiscoveredClass(
                source_key="calendar_event:456",
                calendar_event_id=456,
                title="Pilates Mat",
                class_date=date(2026, 7, 9),
                start_time=time(8, 0),
                instructor_name="Instructor",
                category="Pilates",
                duration_minutes=60,
                is_full=False,
                is_booked_at_gym=False,
                can_book_event=True,
                raw_summary="08:00 AM\n(60 min)\nPilates Mat\nInstructor\nPilates\nBook Now",
                first_seen_at=local_now(),
                last_seen_at=local_now(),
            )
            session.add(discovered_class)
            session.commit()
            session.refresh(discovered_class)
            discovered_class_id = discovered_class.id

        self.assertIsNotNone(discovered_class_id)

        payload = select_discovered_class(discovered_class_id)
        self.assertEqual(456, payload["calendar_event_id"])
        self.assertEqual("pending", payload["status"])

        with Session(app_db.engine) as session:
            job = session.get(BookingJob, payload["id"])

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(456, job.calendar_event_id)
        self.assertEqual(discovered_class_id, job.discovered_class_id)

    def test_clear_week_watched_classes_cancels_only_clearable_manual_jobs(self) -> None:
        now = local_now()
        week_start = date(2026, 7, 12)
        job_ids: dict[str, int] = {}

        with Session(app_db.engine) as session:
            def add_job(
                key: str,
                *,
                class_date: date,
                status: BookingStatus,
                rule_name: str = MANUAL_SELECTION_RULE_NAME,
            ) -> None:
                discovered_class = DiscoveredClass(
                    source_key=f"clear-week:{key}",
                    calendar_event_id=1000 + len(job_ids),
                    title=f"Class {key}",
                    class_date=class_date,
                    start_time=time(8, 0),
                    instructor_name="Coach",
                    category="Fitness",
                    duration_minutes=45,
                    is_full=False,
                    is_booked_at_gym=False,
                    can_book_event=True,
                    raw_summary="08:00 AM\n(45 min)\nClass\nCoach\nFitness\nBook Now",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(discovered_class)
                session.flush()
                job = BookingJob(
                    rule_name=rule_name,
                    target_match_text=discovered_class.title,
                    class_date=class_date,
                    window_start_time=discovered_class.start_time,
                    window_end_time=discovered_class.start_time,
                    expected_run_at=now + timedelta(hours=1),
                    calendar_event_id=discovered_class.calendar_event_id,
                    discovered_class_id=discovered_class.id,
                    status=status,
                    last_message=f"Initial {key}",
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
                session.flush()
                assert job.id is not None
                job_ids[key] = job.id

            add_job("pending", class_date=week_start, status=BookingStatus.PENDING)
            add_job(
                "not-yet-open",
                class_date=date(2026, 7, 18),
                status=BookingStatus.NOT_YET_OPEN,
            )
            add_job("booked", class_date=date(2026, 7, 13), status=BookingStatus.BOOKED)
            add_job("canceled", class_date=date(2026, 7, 14), status=BookingStatus.CANCELED)
            add_job(
                "dry-run",
                class_date=date(2026, 7, 15),
                status=BookingStatus.PENDING,
                rule_name=DRY_RUN_SELECTION_RULE_NAME,
            )
            add_job("outside-week", class_date=date(2026, 7, 19), status=BookingStatus.PENDING)
            session.commit()

        payload = clear_week_watched_classes(week_start)

        self.assertEqual("2026-07-12", payload["week_start"])
        self.assertEqual("2026-07-18", payload["week_end"])
        self.assertEqual(2, payload["cleared_count"])

        with Session(app_db.engine) as session:
            jobs = {key: session.get(BookingJob, job_id) for key, job_id in job_ids.items()}
            discovered_classes = list(session.exec(select(DiscoveredClass)).all())

        self.assertEqual(6, len(discovered_classes))
        self.assertEqual(BookingStatus.CANCELED, jobs["pending"].status)
        self.assertEqual(BookingStatus.CANCELED, jobs["not-yet-open"].status)
        self.assertEqual(BookingStatus.BOOKED, jobs["booked"].status)
        self.assertEqual(BookingStatus.CANCELED, jobs["canceled"].status)
        self.assertEqual(BookingStatus.PENDING, jobs["dry-run"].status)
        self.assertEqual(BookingStatus.PENDING, jobs["outside-week"].status)
        self.assertEqual(
            "Cleared from week view for 2026-07-12",
            jobs["pending"].last_message,
        )

    def test_weekly_schedule_materializes_rolling_discovery_horizon(self) -> None:
        now = local_now()
        current_week_class_id = None
        next_week_class_id = None

        with Session(app_db.engine) as session:
            item = WeeklyScheduleItem(
                scope_key=weekly_schedule_scope_key(
                    weekday=1,
                    start_time=time(8, 0),
                    title="Yoga Level 1",
                ),
                weekday=1,
                start_time=time(8, 0),
                title="Yoga Level 1",
                title_key="yoga-level-1",
                category="Yoga",
                instructor_name="Coach",
                created_at=now,
                updated_at=now,
            )
            session.add(item)

            for key, class_date in [
                ("current", date(2026, 7, 6)),
                ("next", date(2026, 7, 13)),
            ]:
                discovered_class = DiscoveredClass(
                    source_key=f"weekly:{key}",
                    calendar_event_id=2000 if key == "current" else 2001,
                    title="Yoga Level 1",
                    class_date=class_date,
                    start_time=time(8, 0),
                    instructor_name="Coach",
                    category="Yoga",
                    duration_minutes=60,
                    is_full=False,
                    is_booked_at_gym=False,
                    can_book_event=True,
                    raw_summary="08:00 AM\n(60 min)\nYoga Level 1\nCoach\nYoga\nBook Now",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(discovered_class)
                session.flush()
                if key == "current":
                    current_week_class_id = discovered_class.id
                else:
                    next_week_class_id = discovered_class.id

            session.commit()

        with patch("app.weekly_schedule.local_today", return_value=date(2026, 7, 6)):
            payload = materialize_weekly_schedule()

        self.assertEqual(2, payload["created_count"])
        self.assertEqual("2026-07-06", payload["range_start"])
        self.assertEqual("2026-08-03", payload["range_end"])

        with Session(app_db.engine) as session:
            jobs = list(session.exec(select(BookingJob)).all())

        self.assertEqual(2, len(jobs))
        self.assertEqual(
            {current_week_class_id, next_week_class_id},
            {job.discovered_class_id for job in jobs},
        )
        self.assertTrue(all(job.last_message == "Selected from weekly schedule" for job in jobs))

    def test_rolling_horizon_skip_applies_to_only_one_week(self) -> None:
        now = local_now()
        scope_key = weekly_schedule_scope_key(
            weekday=5,
            start_time=time(10, 30),
            title="Yoga Level 1",
        )
        with Session(app_db.engine) as session:
            session.add(
                WeeklyScheduleItem(
                    scope_key=scope_key,
                    weekday=5,
                    start_time=time(10, 30),
                    title="Yoga Level 1",
                    title_key="yoga-level-1",
                    category="Yoga",
                    instructor_name="Coach",
                    created_at=now,
                    updated_at=now,
                )
            )
            class_ids = {}
            for class_date in [date(2026, 7, 17), date(2026, 7, 24)]:
                discovered_class = DiscoveredClass(
                    source_key=f"weekly:skip:{class_date.isoformat()}",
                    calendar_event_id=5000 + class_date.day,
                    title="Yoga Level 1",
                    class_date=class_date,
                    start_time=time(10, 30),
                    instructor_name="Coach",
                    category="Yoga",
                    duration_minutes=60,
                    is_full=False,
                    is_booked_at_gym=False,
                    can_book_event=True,
                    raw_summary="10:30 AM\n(60 min)\nYoga Level 1\nCoach\nYoga\nBook Now",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(discovered_class)
                session.flush()
                class_ids[class_date] = discovered_class.id
            session.commit()

        skip_weekly_schedule_occurrence(class_ids[date(2026, 7, 17)])
        with patch("app.weekly_schedule.local_today", return_value=date(2026, 7, 17)):
            payload = materialize_weekly_schedule()

        self.assertEqual(1, payload["created_count"])
        self.assertEqual(1, payload["skipped_count"])
        with Session(app_db.engine) as session:
            jobs = list(session.exec(select(BookingJob)).all())
            skips = list(session.exec(select(WeeklyScheduleSkip)).all())
        self.assertEqual(1, len(jobs))
        self.assertEqual(class_ids[date(2026, 7, 24)], jobs[0].discovered_class_id)
        self.assertEqual(date(2026, 7, 12), skips[0].week_start)

    def test_recurring_class_can_skip_and_resume_one_week(self) -> None:
        now = local_now()
        class_date = date(2026, 7, 17)
        scope_key = weekly_schedule_scope_key(
            weekday=5,
            start_time=time(10, 30),
            title="Yoga Level 1",
        )

        with Session(app_db.engine) as session:
            item = WeeklyScheduleItem(
                scope_key=scope_key,
                weekday=5,
                start_time=time(10, 30),
                title="Yoga Level 1",
                title_key="yoga-level-1",
                category="Yoga",
                instructor_name="Coach",
                created_at=now,
                updated_at=now,
            )
            discovered_class = DiscoveredClass(
                source_key="weekly:skip-one-week",
                calendar_event_id=4000,
                title="Yoga Level 1",
                class_date=class_date,
                start_time=time(10, 30),
                instructor_name="Coach",
                category="Yoga",
                duration_minutes=60,
                is_full=False,
                is_booked_at_gym=False,
                can_book_event=True,
                raw_summary="10:30 AM\n(60 min)\nYoga Level 1\nCoach\nYoga\nBook Now",
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(item)
            session.add(discovered_class)
            session.flush()
            job = BookingJob(
                rule_name=MANUAL_SELECTION_RULE_NAME,
                target_match_text=discovered_class.title,
                class_date=discovered_class.class_date,
                window_start_time=discovered_class.start_time,
                window_end_time=discovered_class.start_time,
                expected_run_at=now + timedelta(hours=1),
                calendar_event_id=discovered_class.calendar_event_id,
                discovered_class_id=discovered_class.id,
                status=BookingStatus.PENDING,
                last_message="Selected from weekly schedule",
                capture_network=True,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            session.refresh(discovered_class)
            session.refresh(job)
            discovered_class_id = discovered_class.id
            job_id = job.id

        assert discovered_class_id is not None
        assert job_id is not None
        skipped = skip_weekly_schedule_occurrence(discovered_class_id)

        self.assertEqual([job_id], skipped["canceled_job_ids"])
        self.assertEqual("2026-07-12", skipped["skip"]["week_start"])
        self.assertEqual(1, len(skipped["schedule"]["skips"]))

        with patch("app.weekly_schedule.local_today", return_value=date(2026, 7, 13)):
            materialized = materialize_weekly_schedule()

        self.assertEqual(0, materialized["created_count"])
        self.assertEqual(1, materialized["skipped_count"])
        with Session(app_db.engine) as session:
            skipped_job = session.get(BookingJob, job_id)
            schedule_items = list(session.exec(select(WeeklyScheduleItem)).all())
            skips = list(session.exec(select(WeeklyScheduleSkip)).all())

        assert skipped_job is not None
        self.assertEqual(BookingStatus.CANCELED, skipped_job.status)
        self.assertFalse(skipped_job.capture_network)
        self.assertEqual(1, len(schedule_items))
        self.assertEqual(1, len(skips))

        with patch("app.weekly_schedule.local_today", return_value=date(2026, 7, 13)):
            resumed = resume_weekly_schedule_occurrence(discovered_class_id)

        self.assertTrue(resumed["removed"])
        with Session(app_db.engine) as session:
            resumed_job = session.get(BookingJob, job_id)
            skips = list(session.exec(select(WeeklyScheduleSkip)).all())

        assert resumed_job is not None
        self.assertEqual(BookingStatus.PENDING, resumed_job.status)
        self.assertEqual("Resumed recurring class for this week", resumed_job.last_message)
        self.assertEqual([], skips)

    def test_replace_weekly_schedule_from_week_uses_watched_classes(self) -> None:
        now = local_now()
        week_start = date(2026, 7, 5)

        with Session(app_db.engine) as session:
            old_item = WeeklyScheduleItem(
                scope_key=weekly_schedule_scope_key(
                    weekday=2,
                    start_time=time(9, 0),
                    title="Old Class",
                ),
                weekday=2,
                start_time=time(9, 0),
                title="Old Class",
                title_key="old-class",
                created_at=now,
                updated_at=now,
            )
            session.add(old_item)

            watched_class = DiscoveredClass(
                source_key="weekly-template:watched",
                calendar_event_id=3000,
                title="Zumba",
                class_date=date(2026, 7, 7),
                start_time=time(9, 15),
                instructor_name="Fortner",
                category="Zumba",
                duration_minutes=60,
                is_full=False,
                is_booked_at_gym=False,
                can_book_event=True,
                raw_summary="09:15 AM\n(60 min)\nZumba\nFortner\nZumba\nBook Now",
                first_seen_at=now,
                last_seen_at=now,
            )
            unwatched_class = DiscoveredClass(
                source_key="weekly-template:unwatched",
                calendar_event_id=3001,
                title="Pilates Mat",
                class_date=date(2026, 7, 8),
                start_time=time(8, 0),
                instructor_name="Coach",
                category="Pilates",
                duration_minutes=60,
                is_full=False,
                is_booked_at_gym=False,
                can_book_event=True,
                raw_summary="08:00 AM\n(60 min)\nPilates Mat\nCoach\nPilates\nBook Now",
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(watched_class)
            session.add(unwatched_class)
            session.flush()

            session.add(
                BookingJob(
                    rule_name=MANUAL_SELECTION_RULE_NAME,
                    target_match_text=watched_class.title,
                    class_date=watched_class.class_date,
                    window_start_time=watched_class.start_time,
                    window_end_time=watched_class.start_time,
                    expected_run_at=now + timedelta(hours=1),
                    calendar_event_id=watched_class.calendar_event_id,
                    discovered_class_id=watched_class.id,
                    status=BookingStatus.PENDING,
                    last_message="Selected from admin calendar",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with patch("app.weekly_schedule.local_today", return_value=date(2026, 7, 6)):
            payload = replace_weekly_schedule_from_week(week_start)

        self.assertTrue(payload["schedule"]["active"])
        self.assertEqual(1, len(payload["schedule"]["items"]))
        self.assertEqual("Zumba", payload["schedule"]["items"][0]["title"])
        self.assertEqual("Fortner", payload["schedule"]["items"][0]["instructor_name"])

        with Session(app_db.engine) as session:
            items = list(session.exec(select(WeeklyScheduleItem)).all())

        self.assertEqual(1, len(items))
        self.assertEqual("Zumba", items[0].title)


if __name__ == "__main__":
    unittest.main()
