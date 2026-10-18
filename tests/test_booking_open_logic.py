from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.discovery import parse_booking_open_hint
from app.jobs.book_class import (
    _calculate_retry_at,
    _format_notification_subject,
    _should_send_notification,
)
from app.models import BookingStatus


class BookingOpenLogicTests(unittest.TestCase):
    def test_parse_booking_open_hint_extracts_exact_datetime(self) -> None:
        parsed, raw_when = parse_booking_open_hint(
            "Yoga Level 1 can only be booked starting Sun, Jul 05 at 7:00 PM.",
            now=datetime(2026, 7, 2, 9, 30, 0),
        )

        self.assertEqual(datetime(2026, 7, 5, 19, 0, 0), parsed)
        self.assertEqual("Sun, Jul 05 at 7:00 PM", raw_when)

    def test_calculate_retry_at_uses_exact_site_hint_with_buffer(self) -> None:
        fake_settings = SimpleNamespace(
            retry_after_open_seconds=3,
            classes_open_hour=6,
            classes_open_minute=0,
            not_yet_open_fallback_minutes=15,
        )
        with (
            patch("app.jobs.book_class.get_settings", return_value=fake_settings),
            patch("app.jobs.book_class.local_now", return_value=datetime(2026, 7, 2, 12, 0, 0)),
        ):
            retry_at = _calculate_retry_at(
                available_at_hint=datetime(2026, 7, 5, 19, 0, 0),
                available_at_hint_exact=True,
            )

        self.assertEqual(datetime(2026, 7, 5, 19, 0, 3), retry_at)

    def test_calculate_retry_at_uses_configured_open_time_for_date_only_hint(self) -> None:
        fake_settings = SimpleNamespace(
            retry_after_open_seconds=3,
            classes_open_hour=6,
            classes_open_minute=0,
            not_yet_open_fallback_minutes=15,
        )
        with (
            patch("app.jobs.book_class.get_settings", return_value=fake_settings),
            patch("app.jobs.book_class.local_now", return_value=datetime(2026, 7, 2, 12, 0, 0)),
        ):
            retry_at = _calculate_retry_at(
                available_at_hint=datetime(2026, 7, 5, 0, 0, 0),
                available_at_hint_exact=False,
            )

        self.assertEqual(datetime(2026, 7, 5, 6, 0, 3), retry_at)

    def test_calculate_retry_at_uses_backoff_after_exact_open_time_has_passed(self) -> None:
        fake_settings = SimpleNamespace(
            retry_after_open_seconds=3,
            classes_open_hour=6,
            classes_open_minute=0,
            not_yet_open_fallback_minutes=15,
        )
        with (
            patch("app.jobs.book_class.get_settings", return_value=fake_settings),
            patch("app.jobs.book_class.local_now", return_value=datetime(2026, 7, 5, 19, 0, 5)),
        ):
            retry_at = _calculate_retry_at(
                available_at_hint=datetime(2026, 7, 5, 19, 0, 0),
                available_at_hint_exact=True,
                overdue_retry_count=1,
            )

        self.assertEqual(datetime(2026, 7, 5, 19, 1, 5), retry_at)

    def test_calculate_retry_at_returns_none_when_overdue_retries_are_exhausted(self) -> None:
        fake_settings = SimpleNamespace(
            retry_after_open_seconds=3,
            classes_open_hour=6,
            classes_open_minute=0,
            not_yet_open_fallback_minutes=15,
        )
        with (
            patch("app.jobs.book_class.get_settings", return_value=fake_settings),
            patch("app.jobs.book_class.local_now", return_value=datetime(2026, 7, 5, 19, 6, 30)),
        ):
            retry_at = _calculate_retry_at(
                available_at_hint=datetime(2026, 7, 5, 19, 0, 0),
                available_at_hint_exact=True,
                overdue_retry_count=3,
            )

        self.assertIsNone(retry_at)

    def test_format_notification_subject_uses_concise_booked_copy(self) -> None:
        subject = _format_notification_subject(
            class_name="Yoga Level 1",
            status="booked",
            class_date=date(2026, 7, 5),
            class_time=time(8, 15),
        )

        self.assertEqual("BookGym: Yoga Level 1 booked for Sun Jul 05 at 8:15 AM", subject)

    def test_format_notification_subject_uses_status_specific_copy(self) -> None:
        subject = _format_notification_subject(
            class_name="Yoga Level 1",
            status="dry_run",
            class_date=date(2026, 7, 5),
            class_time=time(8, 15),
        )

        self.assertEqual("BookGym: Dry run for Yoga Level 1 on Sun Jul 05 at 8:15 AM", subject)

    def test_should_send_notification_skips_not_yet_open(self) -> None:
        self.assertFalse(_should_send_notification(BookingStatus.NOT_YET_OPEN))
        self.assertTrue(_should_send_notification(BookingStatus.BOOKED))


if __name__ == "__main__":
    unittest.main()
