from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.adapters.base import BookingResult
from app.adapters.gym_site import ExampleGymAdapter


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com/calendar?section=Calendar"
        self.screenshots: list[tuple[str, bool]] = []
        self.closed = False

    async def screenshot(self, path: str, full_page: bool = False) -> None:
        self.screenshots.append((path, full_page))

    async def goto(self, url: str) -> None:
        self.url = url

    async def wait_for_load_state(self, _state: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def wait_for_timeout(self, _timeout_ms: int) -> None:
        return None


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


class _FakeClickable:
    def __init__(self) -> None:
        self.clicks = 0

    async def click(self, *args, **kwargs) -> None:
        self.clicks += 1


class GymSiteAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_result_message_uses_plain_booking_confirmation(self) -> None:
        adapter = ExampleGymAdapter()

        with patch.object(
            adapter,
            "_page_contains_success_signal",
            AsyncMock(return_value=True),
        ):
            message = await adapter._build_result_message(
                page=object(),
                class_name="BodyBalance",
                seat_label="33",
            )

        self.assertEqual("Seat 33 is confirmed for BodyBalance.", message)
        self.assertNotIn("success signal", message.lower())
        self.assertNotIn("strategy", message.lower())
        self.assertNotIn("first available match", message.lower())

    async def test_post_booking_state_advances_immediately_when_seat_prompt_appears(self) -> None:
        adapter = ExampleGymAdapter()
        prompt = SimpleNamespace(count=AsyncMock(return_value=1))
        seat_modal = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            get_by_text=lambda *_args, **_kwargs: prompt,
        )
        modals = SimpleNamespace(
            count=AsyncMock(return_value=1),
            nth=lambda _index: seat_modal,
        )
        page = SimpleNamespace(
            locator=lambda _selector: modals,
            wait_for_timeout=AsyncMock(),
        )

        with (
            patch.object(adapter, "_detect_not_yet_open", AsyncMock(return_value=None)),
            patch.object(adapter, "_detect_non_cancelable_confirmation", AsyncMock(return_value=None)),
            patch.object(adapter, "_detect_booking_success_confirmation", AsyncMock(return_value=None)),
        ):
            state, value = await adapter._wait_for_post_booking_state(page=page)

        self.assertEqual("seat_selection", state)
        self.assertIs(seat_modal, value)
        page.wait_for_timeout.assert_not_awaited()

    async def test_booking_details_state_detects_existing_booking_immediately(self) -> None:
        adapter = ExampleGymAdapter()
        cancel_booking_button = _FakeClickable()
        page = SimpleNamespace(wait_for_timeout=AsyncMock())

        with patch.object(
            adapter,
            "_find_clickable_action",
            AsyncMock(side_effect=[None, cancel_booking_button]),
        ):
            state, action = await adapter._wait_for_booking_details_state(
                page=page,
                card=object(),
            )

        self.assertEqual("already_booked", state)
        self.assertIs(cancel_booking_button, action)
        page.wait_for_timeout.assert_not_awaited()

    async def test_success_signal_wait_returns_as_soon_as_confirmation_appears(self) -> None:
        adapter = ExampleGymAdapter()
        page = SimpleNamespace(wait_for_timeout=AsyncMock())

        with patch.object(
            adapter,
            "_page_contains_success_signal",
            AsyncMock(side_effect=[False, True]),
        ):
            confirmed = await adapter._wait_for_success_signal(page=page)

        self.assertTrue(confirmed)
        page.wait_for_timeout.assert_awaited_once_with(100)

    async def test_preview_mode_blocks_booking_before_opening_gym_page(self) -> None:
        adapter = ExampleGymAdapter()
        open_calendar_page = AsyncMock()

        with (
            patch("app.adapters.gym_site.get_settings", return_value=SimpleNamespace(preview_mode=True)),
            patch.object(adapter, "open_calendar_page", open_calendar_page),
        ):
            result = await adapter.book_class(
                context=object(),
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 20),
            )

        self.assertEqual("canceled", result.status)
        self.assertIn("Preview mode blocked", result.message)
        open_calendar_page.assert_not_awaited()

    async def test_preview_mode_blocks_candidate_before_clicking_booking_action(self) -> None:
        adapter = ExampleGymAdapter()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 7, 20),
            slot_time=time(8, 15),
            summary="08:15 AM\nYoga Level 1\nBook Now",
            calendar_event_id=1234,
        )
        find_action = AsyncMock()

        with (
            patch("app.adapters.gym_site.get_settings", return_value=SimpleNamespace(preview_mode=True)),
            patch.object(adapter, "_find_clickable_action", find_action),
        ):
            result = await adapter.book_candidate(
                page=object(),
                candidate=candidate,
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 20),
            )

        self.assertEqual("canceled", result.status)
        self.assertIn("No gym action was clicked", result.message)
        self.assertEqual(1234, result.matched_calendar_event_id)
        find_action.assert_not_awaited()

    async def test_book_candidate_reports_missing_details_actions_without_assuming_full(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        book_now_button = _FakeClickable()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 7, 31),
            slot_time=time(10, 30),
            summary="10:30 AM\nYoga Level 1\nBook Now",
            calendar_event_id=6590306,
        )

        with (
            patch("app.adapters.gym_site.get_settings", return_value=SimpleNamespace(preview_mode=False)),
            patch.object(adapter, "_find_clickable_action", AsyncMock(return_value=book_now_button)),
            patch.object(adapter, "_wait_for_active_modal", AsyncMock(return_value=object())),
            patch.object(
                adapter,
                "_wait_for_booking_details_state",
                AsyncMock(return_value=("unavailable", None)),
            ),
        ):
            result = await adapter.book_candidate(
                page=fake_page,
                candidate=candidate,
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 31),
            )

        self.assertEqual("failed", result.status)
        self.assertIn("neither 'Book a Spot' nor 'Cancel Booking'", result.message)
        self.assertEqual(1, book_now_button.clicks)
        self.assertEqual(6590306, result.matched_calendar_event_id)

    async def test_book_candidate_treats_cancel_booking_as_already_booked(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        book_now_button = _FakeClickable()
        cancel_booking_button = _FakeClickable()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 9, 1),
            slot_time=time(10, 30),
            summary="10:30 AM\nBodyBalance\nBook Now",
            calendar_event_id=6913612,
        )

        with (
            patch("app.adapters.gym_site.get_settings", return_value=SimpleNamespace(preview_mode=False)),
            patch.object(adapter, "_find_clickable_action", AsyncMock(return_value=book_now_button)),
            patch.object(adapter, "_wait_for_active_modal", AsyncMock(return_value=object())),
            patch.object(
                adapter,
                "_wait_for_booking_details_state",
                AsyncMock(return_value=("already_booked", cancel_booking_button)),
            ),
        ):
            result = await adapter.book_candidate(
                page=fake_page,
                candidate=candidate,
                class_name="BodyBalance",
                class_date=date(2026, 9, 1),
            )

        self.assertEqual("booked", result.status)
        self.assertIn("already booked at the gym", result.message)
        self.assertIn("No additional booking was attempted", result.message)
        self.assertEqual(0, cancel_booking_button.clicks)
        self.assertEqual(6913612, result.matched_calendar_event_id)

    async def test_ensure_logged_in_uses_saved_credentials_when_session_is_logged_out(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        fake_context = _FakeContext(fake_page)

        with (
            patch.object(adapter, "_is_login_page", AsyncMock(side_effect=[True, False])),
            patch.object(adapter, "_login_with_saved_credentials", AsyncMock(return_value=None)) as login_mock,
            patch(
                "app.adapters.gym_site.resolve_gym_credentials",
                return_value=SimpleNamespace(username="user@example.com", password="secret", source="app"),
            ),
            patch("app.adapters.gym_site.record_login_result") as record_mock,
        ):
            await adapter.ensure_logged_in(fake_context)

        login_mock.assert_awaited_once_with(
            page=fake_page,
            username="user@example.com",
            password="secret",
        )
        record_mock.assert_called_once_with(True)
        self.assertTrue(fake_page.closed)

    async def test_ensure_logged_in_raises_when_no_credentials_are_available(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        fake_context = _FakeContext(fake_page)

        with (
            patch.object(adapter, "_is_login_page", AsyncMock(return_value=True)),
            patch("app.adapters.gym_site.resolve_gym_credentials", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "no saved gym credentials"):
                await adapter.ensure_logged_in(fake_context)

        self.assertTrue(fake_page.closed)

    async def test_book_class_passes_seat_selection_into_book_candidate(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        fake_candidate = object()
        expected_date = date(2026, 7, 5)

        with (
            patch.object(adapter, "open_calendar_page", AsyncMock(return_value=fake_page)),
            patch.object(adapter, "_find_booking_candidate", AsyncMock(return_value=fake_candidate)),
            patch.object(
                adapter,
                "book_candidate",
                AsyncMock(return_value=BookingResult(status="dry_run", message="ok")),
            ) as book_candidate_mock,
        ):
            result = await adapter.book_class(
                context=object(),
                class_name="Yoga Level 1",
                class_date=expected_date,
                seat_labels_to_try=["33", "35"],
                seat_selection_source="saved scope 1321:yoga",
                require_preferred_seat=True,
                select_any_available_seat=False,
                allow_any_available_fallback=False,
                dry_run=True,
            )

        self.assertEqual("dry_run", result.status)
        book_candidate_mock.assert_awaited_once_with(
            page=fake_page,
            candidate=fake_candidate,
            class_name="Yoga Level 1",
            class_date=expected_date,
            seat_labels_to_try=["33", "35"],
            seat_selection_source="saved scope 1321:yoga",
            require_preferred_seat=True,
            select_any_available_seat=False,
            allow_any_available_fallback=False,
            dry_run=True,
        )


    async def test_book_candidate_falls_back_to_any_available_seat_when_preferences_miss(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        fake_modal = object()
        book_now_button = _FakeClickable()
        book_a_spot_button = _FakeClickable()
        done_button = _FakeClickable()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 7, 15),
            slot_time=time(11, 45),
            summary=(
                '11:45 AM\n'
                '(60 min)\n'
                'Yoga Level 1\n'
                'Dan Ivarie\n'
                'Yoga\n'
                'Book Now'
            ),
        )

        with (
            patch.object(
                adapter,
                "_find_clickable_action",
                AsyncMock(side_effect=[book_now_button, done_button]),
            ),
            patch.object(adapter, "_wait_for_active_modal", AsyncMock(return_value=fake_modal)),
            patch.object(
                adapter,
                "_wait_for_booking_details_state",
                AsyncMock(return_value=("bookable", book_a_spot_button)),
            ),
            patch.object(
                adapter,
                "_wait_for_post_booking_state",
                AsyncMock(return_value=("seat_selection", fake_modal)),
            ),
            patch.object(adapter, "_wait_for_seat_map_ready", AsyncMock(return_value=True)),
            patch.object(adapter, "_detect_not_yet_open", AsyncMock(return_value=None)),
            patch.object(adapter, "_select_seat_from_candidates", AsyncMock(return_value=None)) as preferred_mock,
            patch.object(adapter, "_select_any_available_seat", AsyncMock(return_value="41")) as any_mock,
        ):
            result = await adapter.book_candidate(
                page=fake_page,
                candidate=candidate,
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 15),
                seat_labels_to_try=["33", "34"],
                seat_selection_source="Common Yoga room prefs",
                allow_any_available_fallback=True,
                dry_run=True,
            )

        self.assertEqual("dry_run", result.status)
        self.assertEqual("41", result.selected_seat_label)
        self.assertIn("fell back to any available seat", result.message)
        preferred_mock.assert_awaited_once_with(
            page=fake_page,
            seat_modal=fake_modal,
            seat_labels=["33", "34"],
        )
        any_mock.assert_awaited_once_with(page=fake_page, seat_modal=fake_modal)

    async def test_book_candidate_does_not_fallback_when_strict_preferences_miss(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        fake_modal = object()
        book_now_button = _FakeClickable()
        book_a_spot_button = _FakeClickable()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 7, 15),
            slot_time=time(11, 45),
            summary=(
                "11:45 AM\n"
                "(60 min)\n"
                "Yoga Level 1\n"
                "Dan Ivarie\n"
                "Yoga\n"
                "Book Now"
            ),
        )

        with (
            patch.object(adapter, "_find_clickable_action", AsyncMock(return_value=book_now_button)),
            patch.object(adapter, "_wait_for_active_modal", AsyncMock(return_value=fake_modal)),
            patch.object(
                adapter,
                "_wait_for_booking_details_state",
                AsyncMock(return_value=("bookable", book_a_spot_button)),
            ),
            patch.object(
                adapter,
                "_wait_for_post_booking_state",
                AsyncMock(return_value=("seat_selection", fake_modal)),
            ),
            patch.object(adapter, "_wait_for_seat_map_ready", AsyncMock(return_value=True)),
            patch.object(adapter, "_detect_not_yet_open", AsyncMock(return_value=None)),
            patch.object(adapter, "_select_seat_from_candidates", AsyncMock(return_value=None)) as preferred_mock,
            patch.object(adapter, "_select_any_available_seat", AsyncMock(return_value="22")) as any_mock,
        ):
            result = await adapter.book_candidate(
                page=fake_page,
                candidate=candidate,
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 15),
                seat_labels_to_try=["33", "34"],
                seat_selection_source="Saved for this class slot",
                require_preferred_seat=True,
                allow_any_available_fallback=False,
                dry_run=True,
            )

        self.assertEqual("failed", result.status)
        self.assertIn("could not select any included seat", result.message)
        preferred_mock.assert_awaited_once_with(
            page=fake_page,
            seat_modal=fake_modal,
            seat_labels=["33", "34"],
        )
        any_mock.assert_not_awaited()

    async def test_book_candidate_stops_if_strict_selection_returns_unlisted_seat(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        fake_modal = object()
        book_now_button = _FakeClickable()
        book_a_spot_button = _FakeClickable()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 7, 15),
            slot_time=time(11, 45),
            summary=(
                "11:45 AM\n"
                "(60 min)\n"
                "Yoga Level 1\n"
                "Dan Ivarie\n"
                "Yoga\n"
                "Book Now"
            ),
        )

        with (
            patch.object(adapter, "_find_clickable_action", AsyncMock(return_value=book_now_button)) as action_mock,
            patch.object(adapter, "_wait_for_active_modal", AsyncMock(return_value=fake_modal)),
            patch.object(
                adapter,
                "_wait_for_booking_details_state",
                AsyncMock(return_value=("bookable", book_a_spot_button)),
            ),
            patch.object(
                adapter,
                "_wait_for_post_booking_state",
                AsyncMock(return_value=("seat_selection", fake_modal)),
            ),
            patch.object(adapter, "_wait_for_seat_map_ready", AsyncMock(return_value=True)),
            patch.object(adapter, "_detect_not_yet_open", AsyncMock(return_value=None)),
            patch.object(adapter, "_select_seat_from_candidates", AsyncMock(return_value="22")),
            patch.object(adapter, "_select_any_available_seat", AsyncMock(return_value=None)) as any_mock,
        ):
            result = await adapter.book_candidate(
                page=fake_page,
                candidate=candidate,
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 15),
                seat_labels_to_try=["33", "34"],
                seat_selection_source="Saved for this class slot",
                require_preferred_seat=True,
                allow_any_available_fallback=False,
                dry_run=True,
            )

        self.assertEqual("failed", result.status)
        self.assertEqual("22", result.selected_seat_label)
        self.assertIn("strict mode only allows", result.message)
        self.assertEqual(1, action_mock.await_count)
        any_mock.assert_not_awaited()

    async def test_find_booking_candidate_does_not_fallback_when_exact_slot_is_missing(self) -> None:
        adapter = ExampleGymAdapter()
        sunday_candidate = SimpleNamespace(
            slot_date=date(2026, 7, 12),
            slot_time=time(8, 15),
            summary='08:15 AM\n(60 min)\nYoga Level 1\nHolly Jackson Sigman\nYoga\nBook Now',
        )

        with (
            patch.object(adapter, "_extract_calendar_headers", AsyncMock(return_value=[])),
            patch.object(adapter, "_find_candidate_cards", AsyncMock(return_value=[sunday_candidate])),
        ):
            result = await adapter._find_booking_candidate(
                page=object(),
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 13),
                window_start_time=time(11, 45),
                window_end_time=time(11, 45),
            )

        self.assertIsNone(result)

    async def test_find_booking_candidate_uses_calendar_event_id_for_exact_card(self) -> None:
        adapter = ExampleGymAdapter()
        early_candidate = SimpleNamespace(
            slot_date=date(2026, 7, 13),
            slot_time=time(10, 30),
            summary='10:30 AM\n(60 min)\nYoga Level 1\nCoach A\nYoga\nBook Now',
        )
        target_candidate = SimpleNamespace(
            slot_date=date(2026, 7, 13),
            slot_time=time(11, 45),
            summary='11:45 AM\n(60 min)\nYoga Level 1\nCoach B\nYoga\nBook Now',
        )

        with (
            patch.object(
                adapter,
                "fetch_calendar_events",
                AsyncMock(
                    return_value=[
                        {
                            "CalendarEventId": 111,
                            "EventName": "Yoga Level 1",
                            "StartTime": "2026-07-13T10:30:00.000Z",
                        },
                        {
                            "CalendarEventId": 222,
                            "EventName": "Yoga Level 1",
                            "StartTime": "2026-07-13T11:45:00.000Z",
                        },
                    ]
                ),
            ),
            patch.object(adapter, "_extract_calendar_headers", AsyncMock(return_value=[])),
            patch.object(
                adapter,
                "_find_candidate_cards",
                AsyncMock(return_value=[early_candidate, target_candidate]),
            ),
        ):
            result = await adapter._find_booking_candidate(
                page=object(),
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 13),
                window_start_time=time(11, 45),
                window_end_time=time(11, 45),
                calendar_event_id=222,
            )

        self.assertIs(result, target_candidate)
        self.assertEqual(222, result.calendar_event_id)

    async def test_find_booking_candidate_does_not_fallback_when_calendar_event_card_is_missing(self) -> None:
        adapter = ExampleGymAdapter()
        other_candidate = SimpleNamespace(
            slot_date=date(2026, 7, 13),
            slot_time=time(10, 30),
            summary='10:30 AM\n(60 min)\nYoga Level 1\nCoach A\nYoga\nBook Now',
        )

        with (
            patch.object(
                adapter,
                "fetch_calendar_events",
                AsyncMock(
                    return_value=[
                        {
                            "CalendarEventId": 222,
                            "EventName": "Yoga Level 1",
                            "StartTime": "2026-07-13T11:45:00.000Z",
                        }
                    ]
                ),
            ),
            patch.object(adapter, "_extract_calendar_headers", AsyncMock(return_value=[])),
            patch.object(adapter, "_find_candidate_cards", AsyncMock(return_value=[other_candidate])),
        ):
            result = await adapter._find_booking_candidate(
                page=object(),
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 13),
                window_start_time=None,
                window_end_time=None,
                calendar_event_id=222,
            )

        self.assertIsNone(result)

    async def test_find_booking_candidate_returns_none_when_calendar_event_id_is_absent(self) -> None:
        adapter = ExampleGymAdapter()

        with (
            patch.object(adapter, "fetch_calendar_events", AsyncMock(return_value=[])),
            patch.object(adapter, "_find_candidate_cards", AsyncMock()) as find_cards_mock,
        ):
            result = await adapter._find_booking_candidate(
                page=object(),
                class_name="Yoga Level 1",
                class_date=date(2026, 7, 13),
                window_start_time=time(11, 45),
                window_end_time=time(11, 45),
                calendar_event_id=222,
            )

        self.assertIsNone(result)
        find_cards_mock.assert_not_awaited()

    async def test_book_candidate_treats_immediate_success_as_booked_without_seat_selection(self) -> None:
        adapter = ExampleGymAdapter()
        fake_page = _FakePage()
        book_now_button = _FakeClickable()
        book_a_spot_button = _FakeClickable()
        candidate = SimpleNamespace(
            locator=object(),
            slot_date=date(2026, 7, 7),
            slot_time=None,
            summary="08:00 AM\n(60 min)\nPilates Mat\nHolly Jarratt\nPilates\nBook Now",
        )

        with (
            patch.object(adapter, "_find_clickable_action", AsyncMock(return_value=book_now_button)),
            patch.object(adapter, "_wait_for_active_modal", AsyncMock(return_value=object())),
            patch.object(
                adapter,
                "_wait_for_booking_details_state",
                AsyncMock(return_value=("bookable", book_a_spot_button)),
            ),
            patch.object(
                adapter,
                "_wait_for_post_booking_state",
                AsyncMock(
                    return_value=(
                        "booked",
                        "You are booked for the Pilates Mat on Tue, Jul 07 at 08:00 AM",
                    )
                ),
            ),
        ):
            result = await adapter.book_candidate(
                page=fake_page,
                candidate=candidate,
                class_name="Pilates Mat",
                class_date=date(2026, 7, 7),
            )

        self.assertEqual("booked", result.status)
        self.assertEqual(
            "Pilates Mat was booked successfully. No seat selection was required.",
            result.message,
        )
        self.assertEqual(1, book_now_button.clicks)
        self.assertEqual(1, book_a_spot_button.clicks)
        self.assertTrue(fake_page.screenshots)
        self.assertEqual("Pilates Mat", result.matched_class_title)


if __name__ == "__main__":
    unittest.main()
