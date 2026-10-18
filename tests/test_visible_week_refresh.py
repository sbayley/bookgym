from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session, create_engine, select

from app import db as app_db
from app.main import _visible_week_refreshes, refresh_visible_week
from app.models import DiscoveryEvent


class VisibleWeekRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()
        _visible_week_refreshes.clear()

    def tearDown(self) -> None:
        _visible_week_refreshes.clear()
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()

    @patch("app.main.local_today", return_value=date(2026, 7, 13))
    @patch("app.main.local_now", return_value=datetime(2026, 7, 13, 10, 0, 0))
    @patch("app.main.refresh_discovery")
    def test_stale_future_week_refreshes_once(self, refresh_mock, _now_mock, _today_mock) -> None:
        refresh_mock.return_value = SimpleNamespace(scanned_ranges=1, matched_classes=8)

        result = refresh_visible_week(date(2026, 7, 19))

        self.assertEqual("refreshed", result["status"])
        refresh_mock.assert_called_once_with(
            date_from=date(2026, 7, 19),
            date_to=date(2026, 7, 25),
            stale_only=False,
        )
        with Session(app_db.engine) as session:
            requests = list(
                session.exec(
                    select(DiscoveryEvent).where(
                        DiscoveryEvent.event_type == "view_refresh_requested"
                    )
                ).all()
            )
        self.assertEqual(1, len(requests))

    @patch("app.main.local_today", return_value=date(2026, 7, 13))
    @patch("app.main.local_now", return_value=datetime(2026, 7, 13, 10, 0, 0))
    @patch("app.main.refresh_discovery")
    def test_recent_attempt_skips_refresh(self, refresh_mock, _now_mock, _today_mock) -> None:
        with Session(app_db.engine) as session:
            session.add(
                DiscoveryEvent(
                    event_type="api_range_scanned",
                    message="Fetched this week.",
                    visible_week_start=date(2026, 7, 19),
                    visible_week_end=date(2026, 7, 25),
                    observed_at=datetime(2026, 7, 13, 9, 30, 0),
                )
            )
            session.commit()

        result = refresh_visible_week(date(2026, 7, 19))

        self.assertEqual("fresh", result["status"])
        refresh_mock.assert_not_called()

    @patch("app.main.local_today", return_value=date(2026, 7, 13))
    @patch("app.main.refresh_discovery")
    def test_past_week_never_refreshes(self, refresh_mock, _today_mock) -> None:
        result = refresh_visible_week(date(2026, 7, 5))

        self.assertEqual("past", result["status"])
        refresh_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
