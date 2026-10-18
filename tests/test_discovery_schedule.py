from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from sqlmodel import Session, create_engine

from app import db as app_db
from app.discovery import _date_ranges, _week_needs_refresh, _week_needs_view_refresh
from app.models import DiscoveryEvent


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


class DiscoveryScheduleTests(TempDatabaseTestCase):
    def test_date_ranges_align_to_sunday_started_weeks(self) -> None:
        ranges = _date_ranges(date(2026, 7, 2), date(2026, 7, 15))

        self.assertEqual(
            [
                (date(2026, 6, 28), date(2026, 7, 4)),
                (date(2026, 7, 5), date(2026, 7, 11)),
                (date(2026, 7, 12), date(2026, 7, 18)),
            ],
            ranges,
        )

    def test_week_needs_refresh_when_latest_scan_is_older_than_cutoff(self) -> None:
        week_start = date(2026, 7, 5)
        week_end = date(2026, 7, 11)
        with Session(app_db.engine) as session:
            session.add(
                DiscoveryEvent(
                    event_type="api_range_scanned",
                    message="Fetched this week.",
                    visible_week_start=week_start,
                    visible_week_end=week_end,
                    observed_at=datetime(2026, 7, 1, 9, 0, 0),
                )
            )
            session.commit()

        with Session(app_db.engine) as session:
            needs_refresh = _week_needs_refresh(
                session=session,
                range_start=week_start,
                range_end=week_end,
                refreshed_before=datetime(2026, 7, 2, 9, 0, 0),
            )

        self.assertTrue(needs_refresh)

    def test_week_does_not_need_refresh_when_recent_scan_exists(self) -> None:
        week_start = date(2026, 7, 5)
        week_end = date(2026, 7, 11)
        with Session(app_db.engine) as session:
            session.add(
                DiscoveryEvent(
                    event_type="api_range_scanned",
                    message="Fetched this week.",
                    visible_week_start=week_start,
                    visible_week_end=week_end,
                    observed_at=datetime(2026, 7, 3, 9, 0, 0),
                )
            )
            session.commit()

        with Session(app_db.engine) as session:
            needs_refresh = _week_needs_refresh(
                session=session,
                range_start=week_start,
                range_end=week_end,
                refreshed_before=datetime(2026, 7, 2, 9, 0, 0),
            )

        self.assertFalse(needs_refresh)

    def test_recent_view_refresh_attempt_enforces_cooldown_even_without_success(self) -> None:
        week_start = date(2026, 7, 19)
        week_end = date(2026, 7, 25)
        with Session(app_db.engine) as session:
            session.add(
                DiscoveryEvent(
                    event_type="view_refresh_requested",
                    message="Refresh requested.",
                    visible_week_start=week_start,
                    visible_week_end=week_end,
                    observed_at=datetime(2026, 7, 13, 9, 30, 0),
                )
            )
            session.commit()

        with Session(app_db.engine) as session:
            needs_refresh = _week_needs_view_refresh(
                session=session,
                range_start=week_start,
                range_end=week_end,
                refreshed_before=datetime(2026, 7, 13, 9, 0, 0),
            )

        self.assertFalse(needs_refresh)


if __name__ == "__main__":
    unittest.main()
