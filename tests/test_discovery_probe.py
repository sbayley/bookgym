from __future__ import annotations

from datetime import date, time
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, create_engine

from app import db as app_db
from app.discovery import (
    _live_booking_probe_result,
    _upsert_api_discovered_class,
    probe_discovered_class_booking_status_async,
)
from app.models import DiscoveredClass
from app.time_utils import local_now


class _FakePage:
    def __init__(self) -> None:
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True


class _FakeContext:
    async def storage_state(self, path: str | None = None) -> dict:
        return {}

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    async def new_context(self, storage_state: str | None = None) -> _FakeContext:
        return self._context

    async def close(self) -> None:
        return None


class _FakePlaywrightManager:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._playwright = SimpleNamespace(
            chromium=SimpleNamespace(
                launch=AsyncMock(return_value=browser),
            )
        )

    async def __aenter__(self):
        return self._playwright

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class TempDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.auth_state_path = Path(self.temp_dir.name) / "auth.json"
        self.auth_state_path.write_text("{}", encoding="utf-8")
        self.original_engine = app_db.engine
        app_db.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        app_db.init_db()

    def tearDown(self) -> None:
        app_db.engine = self.original_engine
        self.temp_dir.cleanup()


class DiscoveryProbeTests(TempDatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    def create_discovered_class(self) -> int:
        with Session(app_db.engine) as session:
            discovered_class = DiscoveredClass(
                source_key="calendar_event:456",
                calendar_event_id=456,
                title="Pilates Mat",
                class_date=date(2026, 7, 7),
                start_time=time(8, 0),
                instructor_name="Holly Jarratt",
                category="Pilates",
                duration_minutes=60,
                event_type="in-person",
                booking_type_id=9530,
                resource_id=2407,
                can_show_layout=False,
                seat_title="Spot",
                total_spots=30,
                total_available_spots=18,
                total_booked=12,
                is_full=False,
                can_book_event=True,
                is_booked_at_gym=False,
                gym_booking_status="",
                gym_booking_spot_labels="",
                gym_booking_actions="book-spot",
                raw_summary="08:00 AM\n(60 min)\nPilates Mat\nHolly Jarratt\nPilates\nBook Now",
                first_seen_at=local_now(),
                last_seen_at=local_now(),
            )
            session.add(discovered_class)
            session.commit()
            session.refresh(discovered_class)
            assert discovered_class.id is not None
            return discovered_class.id

    async def test_probe_uses_live_calendar_state_to_mark_class_booked(self) -> None:
        discovered_class_id = self.create_discovered_class()
        fake_page = _FakePage()
        fake_context = _FakeContext()
        fake_browser = _FakeBrowser(fake_context)
        fake_adapter = SimpleNamespace(
            ensure_logged_in=AsyncMock(return_value=None),
            open_calendar_page=AsyncMock(return_value=fake_page),
            check_booking_constraint=AsyncMock(
                return_value={"ok": True, "status": 200, "body": {"message": "Booking constraint check passed."}}
            ),
            fetch_calendar_events=AsyncMock(
                return_value=[
                    {
                        "CalendarEventId": 456,
                        "EventName": "Pilates Mat",
                        "StartTime": "2026-07-07T08:00:00",
                        "Instructors": [{"InstructorFullName": "Holly Jarratt"}],
                        "Location": "Pilates",
                        "Duration": 60,
                        "EventType": "in-person",
                        "BookingTypeId": 9530,
                        "ResourceId": 2407,
                        "CanShowLayout": False,
                        "SeatTitle": "Spot",
                        "TotalSpots": 30,
                        "TotalAvailableSpots": 18,
                        "TotalBooked": 12,
                        "IsFull": False,
                        "CanBookEvent": True,
                        "BookingStatus": "Booked",
                        "LayoutItemIdTitles": [],
                        "BottomSheetButtons": ["cancel-booking"],
                        "DeepLinkUrl": "https://example.com/event/456",
                    }
                ]
            ),
        )

        with (
            patch("app.discovery.ensure_auth_state_directory", return_value=self.auth_state_path),
            patch("app.discovery.auth_state_context_kwargs", return_value={"storage_state": str(self.auth_state_path)}),
            patch("app.discovery.ExampleGymAdapter", return_value=fake_adapter),
            patch("app.discovery.async_playwright", return_value=_FakePlaywrightManager(fake_browser)),
        ):
            discovered_class = await probe_discovered_class_booking_status_async(
                discovered_class_id=discovered_class_id,
                headless=True,
            )

        self.assertEqual("booked_at_gym", discovered_class.booking_probe_status)
        self.assertTrue(discovered_class.is_booked_at_gym)
        self.assertEqual("Booked", discovered_class.gym_booking_status)
        self.assertIn("already booked at the gym", discovered_class.booking_probe_message)
        fake_adapter.fetch_calendar_events.assert_awaited_once_with(
            page=fake_page,
            start_date=date(2026, 7, 5),
            end_date=date(2026, 7, 11),
        )

    def test_api_refresh_clears_stale_booked_probe_state_when_class_is_available(self) -> None:
        discovered_class_id = self.create_discovered_class()
        probed_at = local_now()

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, discovered_class_id)
            assert discovered_class is not None
            discovered_class.is_booked_at_gym = True
            discovered_class.gym_booking_status = "confirmed"
            discovered_class.booking_probe_status = "booked_at_gym"
            discovered_class.booking_probe_message = "Live calendar state shows this class is already booked at the gym."
            discovered_class.last_probed_at = probed_at
            session.add(discovered_class)
            session.commit()

        refreshed_at = local_now()
        available_event = {
            "CalendarEventId": 456,
            "EventName": "Pilates Mat",
            "StartTime": "2026-07-07T08:00:00",
            "Instructors": [{"InstructorFullName": "Holly Jarratt"}],
            "Location": "Pilates",
            "Duration": 60,
            "EventType": "in-person",
            "BookingTypeId": 9530,
            "ResourceId": 2407,
            "CanShowLayout": False,
            "SeatTitle": "Spot",
            "TotalSpots": 30,
            "TotalAvailableSpots": 18,
            "TotalBooked": 12,
            "IsFull": False,
            "CanBookEvent": True,
            "BookingStatus": "",
            "LayoutItemIdTitles": [],
            "BottomSheetButtons": ["book-spot"],
            "DeepLinkUrl": "https://example.com/event/456",
        }

        with Session(app_db.engine) as session:
            refreshed_class = _upsert_api_discovered_class(
                session=session,
                event=available_event,
                observed_at=refreshed_at,
            )
            session.commit()
            assert refreshed_class is not None
            refreshed_class_id = refreshed_class.id

        with Session(app_db.engine) as session:
            refreshed_class = session.get(DiscoveredClass, refreshed_class_id)

        assert refreshed_class is not None
        self.assertFalse(refreshed_class.is_booked_at_gym)
        self.assertEqual("", refreshed_class.gym_booking_status)
        self.assertEqual("", refreshed_class.booking_probe_status)
        self.assertEqual("", refreshed_class.booking_probe_message)
        self.assertIsNone(refreshed_class.last_probed_at)
        self.assertIn("Book Now", refreshed_class.raw_summary)


    def test_live_booking_probe_result_detects_waitlist(self) -> None:
        result = _live_booking_probe_result(
            {
                "BookingStatus": "Waitlisted",
                "BottomSheetButtons": ["cancel-waitlist"],
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("waitlisted_at_gym", result.status)
        self.assertIn("already waitlisted at the gym", result.message)


if __name__ == "__main__":
    unittest.main()
