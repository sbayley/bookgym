from __future__ import annotations

from datetime import date, time
from pathlib import Path
import tempfile
import unittest

from sqlmodel import Session, create_engine, select

from app import db as app_db
from app.models import DiscoveredClass, SeatPreference
from app.seat_preferences import (
    CLASS_SLOT_SCOPE_KIND,
    ROOM_CATEGORY_SCOPE_KIND,
    SELECTION_MODE_ANY,
    SELECTION_MODE_PREFERENCES,
    SELECTION_MODE_STRICT,
    configured_default_seat_labels,
    reset_class_slot_seat_preferences,
    resolve_booking_seat_selection,
    save_seat_preferences,
    seat_preferences_payload,
)
from app.time_utils import local_now


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

    def create_discovered_class(
        self,
        *,
        source_key: str,
        title: str = "BodyPump",
        category: str = "BodyPump",
        layout_id: int | None = 1231,
        class_date: date = date(2026, 7, 9),
        start_time: time | None = time(8, 0),
        api_payload: str = "",
    ) -> int:
        with Session(app_db.engine) as session:
            discovered_class = DiscoveredClass(
                source_key=source_key,
                calendar_event_id=123456,
                layout_id=layout_id,
                title=title,
                class_date=class_date,
                start_time=start_time,
                instructor_name="Instructor",
                category=category,
                duration_minutes=60,
                is_full=False,
                is_booked_at_gym=False,
                can_book_event=True,
                raw_summary="08:00 AM\n(60 min)\nBodyPump\nInstructor\nBodyPump\nBook Now",
                api_payload=api_payload,
                first_seen_at=local_now(),
                last_seen_at=local_now(),
            )
            session.add(discovered_class)
            session.commit()
            session.refresh(discovered_class)
            assert discovered_class.id is not None
            return discovered_class.id


class SeatPreferenceTests(TempDatabaseTestCase):
    def test_preferences_are_saved_for_recurring_class_slot_by_default(self) -> None:
        first_id = self.create_discovered_class(
            source_key="calendar_event:1",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 10),
            start_time=time(8, 0),
        )
        next_week_id = self.create_discovered_class(
            source_key="calendar_event:2",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 17),
            start_time=time(8, 0),
        )

        with Session(app_db.engine) as session:
            first_class = session.get(DiscoveredClass, first_id)
            next_week_class = session.get(DiscoveredClass, next_week_id)
            assert first_class is not None
            assert next_week_class is not None

            saved = save_seat_preferences(
                session=session,
                discovered_class=first_class,
                included_seats=["35", "33"],
                excluded_seats=["12"],
            )
            session.commit()

            carried = seat_preferences_payload(session=session, discovered_class=next_week_class)

        self.assertEqual(
            "class-slot:1321:yoga:4:08:00:yoga-level-1",
            saved["preference_scope"]["scope_key"],
        )
        self.assertEqual("explicit", saved["seat_preference_source"]["source_type"])
        self.assertEqual(["35", "33"], carried["seat_preferences"]["included_seats"])
        self.assertEqual(["12"], carried["seat_preferences"]["excluded_seats"])
        self.assertEqual("explicit", carried["seat_preference_source"]["source_type"])

    def test_preferences_can_save_when_layout_only_exists_in_api_payload(self) -> None:
        class_id = self.create_discovered_class(
            source_key="calendar_event:api-fallback",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=None,
            api_payload='{"LayoutId": 1321, "CanShowLayout": true, "EquipmentName": "Spot"}',
        )

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            saved = save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=["33"],
                excluded_seats=[],
            )
            session.commit()
            session.refresh(discovered_class)

        self.assertEqual("class-slot:1321:yoga:3:08:00:yoga-level-1", saved["preference_scope"]["scope_key"])
        self.assertEqual(1321, discovered_class.layout_id)

    def test_saving_empty_class_slot_preferences_means_any_seat(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:3")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=["33"],
                excluded_seats=["12"],
            )
            session.commit()

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=[],
                excluded_seats=[],
            )
            session.commit()

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)
            remaining = session.exec(select(SeatPreference)).all()

        self.assertEqual([], plan.seat_labels)
        self.assertTrue(plan.select_any_available_seat)
        self.assertEqual(1, len(remaining))
        self.assertEqual(SELECTION_MODE_ANY, remaining[0].selection_mode)

    def test_booking_resolution_prefers_included_seats(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:4")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=["35", "33"],
                excluded_seats=["12"],
            )
            session.commit()

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)

        self.assertEqual(["35", "33"], plan.seat_labels)
        self.assertTrue(plan.hard_constraint)
        self.assertFalse(plan.allow_any_available_fallback)
        self.assertEqual("Saved for this class slot", plan.source)

    def test_default_seat_list_is_strict_when_no_saved_preference(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:default-strict")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)

        self.assertEqual(configured_default_seat_labels(), plan.seat_labels)
        self.assertTrue(plan.seat_labels)
        self.assertTrue(plan.hard_constraint)
        self.assertFalse(plan.allow_any_available_fallback)

    def test_prefer_first_mode_can_fall_back_to_any_available_seat(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:prefer-first")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=["35", "33"],
                excluded_seats=[],
                selection_mode=SELECTION_MODE_PREFERENCES,
            )
            session.commit()

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)

        self.assertEqual(["35", "33"], plan.seat_labels)
        self.assertFalse(plan.hard_constraint)
        self.assertTrue(plan.allow_any_available_fallback)

    def test_strict_mode_requires_included_seats(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:strict-empty")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            with self.assertRaisesRegex(ValueError, "needs at least one preferred seat"):
                save_seat_preferences(
                    session=session,
                    discovered_class=discovered_class,
                    included_seats=[],
                    excluded_seats=[],
                    selection_mode=SELECTION_MODE_STRICT,
                )

    def test_strict_mode_books_only_included_seats(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:strict")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=["35", "33"],
                excluded_seats=[],
                selection_mode=SELECTION_MODE_STRICT,
            )
            session.commit()

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)
            payload = seat_preferences_payload(session=session, discovered_class=discovered_class)

        self.assertEqual(["35", "33"], plan.seat_labels)
        self.assertTrue(plan.hard_constraint)
        self.assertFalse(plan.allow_any_available_fallback)
        self.assertEqual(SELECTION_MODE_STRICT, payload["seat_preferences"]["selection_mode"])

    def test_booking_resolution_keeps_explicit_included_order(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:4b")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=["33", "23", "35"],
                excluded_seats=[],
            )
            session.commit()

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)

        self.assertEqual(["33", "23", "35"], plan.seat_labels)

    def test_booking_resolution_filters_default_seats_when_only_exclusions_exist(self) -> None:
        class_id = self.create_discovered_class(source_key="calendar_event:5")

        with Session(app_db.engine) as session:
            discovered_class = session.get(DiscoveredClass, class_id)
            assert discovered_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=discovered_class,
                included_seats=[],
                excluded_seats=["32", "33"],
            )
            session.commit()

            plan = resolve_booking_seat_selection(session=session, discovered_class=discovered_class)

        self.assertFalse(plan.hard_constraint)
        self.assertNotIn("32", plan.seat_labels)
        self.assertNotIn("33", plan.seat_labels)
        self.assertTrue(plan.seat_labels[:2] == ["34", "35"])

    def test_missing_slot_preferences_infer_from_related_class_slot(self) -> None:
        source_yoga_id = self.create_discovered_class(
            source_key="calendar_event:source-yoga",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 10),
            start_time=time(9, 15),
        )
        target_yoga_id = self.create_discovered_class(
            source_key="calendar_event:target-yoga",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 11),
            start_time=time(10, 30),
        )

        with Session(app_db.engine) as session:
            source_yoga = session.get(DiscoveredClass, source_yoga_id)
            target_yoga = session.get(DiscoveredClass, target_yoga_id)
            assert source_yoga is not None
            assert target_yoga is not None

            save_seat_preferences(
                session=session,
                discovered_class=source_yoga,
                included_seats=["33", "35"],
                excluded_seats=[],
            )
            session.commit()

            target_payload = seat_preferences_payload(session=session, discovered_class=target_yoga)
            target_plan = resolve_booking_seat_selection(session=session, discovered_class=target_yoga)

        self.assertEqual(["33", "35"], target_payload["seat_preferences"]["included_seats"])
        self.assertFalse(target_payload["seat_preferences"]["has_saved_preferences"])
        self.assertEqual("inferred", target_payload["seat_preference_source"]["source_type"])
        self.assertEqual(["33", "35"], target_plan.seat_labels)
        self.assertTrue(target_plan.hard_constraint)
        self.assertFalse(target_plan.allow_any_available_fallback)

    def test_related_any_seat_slot_does_not_override_room_category_preferences(self) -> None:
        source_yoga_id = self.create_discovered_class(
            source_key="calendar_event:yoga-room-source",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 10),
            start_time=time(10, 30),
        )
        target_yoga_id = self.create_discovered_class(
            source_key="calendar_event:yoga-room-target",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 15),
            start_time=time(11, 45),
        )

        with Session(app_db.engine) as session:
            source_yoga = session.get(DiscoveredClass, source_yoga_id)
            target_yoga = session.get(DiscoveredClass, target_yoga_id)
            assert source_yoga is not None
            assert target_yoga is not None

            save_seat_preferences(
                session=session,
                discovered_class=source_yoga,
                included_seats=["33", "34", "35"],
                excluded_seats=[],
                scope_kind=ROOM_CATEGORY_SCOPE_KIND,
            )
            save_seat_preferences(
                session=session,
                discovered_class=source_yoga,
                included_seats=[],
                excluded_seats=[],
            )
            session.commit()

            target_payload = seat_preferences_payload(session=session, discovered_class=target_yoga)
            target_plan = resolve_booking_seat_selection(session=session, discovered_class=target_yoga)

        self.assertEqual(["33", "34", "35"], target_payload["seat_preferences"]["included_seats"])
        self.assertEqual(ROOM_CATEGORY_SCOPE_KIND, target_payload["seat_preference_source"]["source_kind"])
        self.assertEqual(["33", "34", "35"], target_plan.seat_labels)
        self.assertTrue(target_plan.hard_constraint)
        self.assertFalse(target_plan.allow_any_available_fallback)


    def test_editing_inferred_preferences_creates_class_slot_preference(self) -> None:
        source_yoga_id = self.create_discovered_class(
            source_key="calendar_event:source-yoga-edit",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 10),
            start_time=time(9, 15),
        )
        target_yoga_id = self.create_discovered_class(
            source_key="calendar_event:target-yoga-edit",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 11),
            start_time=time(10, 30),
        )

        with Session(app_db.engine) as session:
            source_yoga = session.get(DiscoveredClass, source_yoga_id)
            target_yoga = session.get(DiscoveredClass, target_yoga_id)
            assert source_yoga is not None
            assert target_yoga is not None

            save_seat_preferences(
                session=session,
                discovered_class=source_yoga,
                included_seats=["33", "35", "34"],
                excluded_seats=[],
            )
            save_seat_preferences(
                session=session,
                discovered_class=target_yoga,
                included_seats=["35", "34"],
                excluded_seats=[],
            )
            session.commit()

            source_payload = seat_preferences_payload(session=session, discovered_class=source_yoga)
            target_payload = seat_preferences_payload(session=session, discovered_class=target_yoga)

        self.assertEqual(["33", "35", "34"], source_payload["seat_preferences"]["included_seats"])
        self.assertEqual(["35", "34"], target_payload["seat_preferences"]["included_seats"])
        self.assertEqual("explicit", target_payload["seat_preference_source"]["source_type"])

    def test_reset_class_slot_preferences_returns_to_inferred_related_preferences(self) -> None:
        source_yoga_id = self.create_discovered_class(
            source_key="calendar_event:source-yoga-reset",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 10),
            start_time=time(9, 15),
        )
        target_yoga_id = self.create_discovered_class(
            source_key="calendar_event:target-yoga-reset",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 11),
            start_time=time(10, 30),
        )

        with Session(app_db.engine) as session:
            source_yoga = session.get(DiscoveredClass, source_yoga_id)
            target_yoga = session.get(DiscoveredClass, target_yoga_id)
            assert source_yoga is not None
            assert target_yoga is not None

            save_seat_preferences(
                session=session,
                discovered_class=source_yoga,
                included_seats=["33", "35"],
                excluded_seats=[],
            )
            save_seat_preferences(
                session=session,
                discovered_class=target_yoga,
                included_seats=[],
                excluded_seats=[],
            )
            session.commit()

            any_plan = resolve_booking_seat_selection(session=session, discovered_class=target_yoga)
            reset_payload = reset_class_slot_seat_preferences(
                session=session,
                discovered_class=target_yoga,
            )
            reset_plan = resolve_booking_seat_selection(session=session, discovered_class=target_yoga)

        self.assertEqual([], any_plan.seat_labels)
        self.assertTrue(any_plan.select_any_available_seat)
        self.assertEqual(["33", "35"], reset_plan.seat_labels)
        self.assertEqual("inferred", reset_payload["seat_preference_source"]["source_type"])
        self.assertFalse(reset_payload["class_slot_seat_preferences"]["has_saved_preferences"])

    def test_room_category_preferences_remain_available_as_legacy_inference_source(self) -> None:
        class_id = self.create_discovered_class(
            source_key="calendar_event:room-source",
            title="Yoga Level 1",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 10),
            start_time=time(9, 15),
        )
        target_id = self.create_discovered_class(
            source_key="calendar_event:room-target",
            title="Yoga Level 2",
            category="Yoga",
            layout_id=1321,
            class_date=date(2026, 7, 11),
            start_time=time(10, 30),
        )

        with Session(app_db.engine) as session:
            source_class = session.get(DiscoveredClass, class_id)
            target_class = session.get(DiscoveredClass, target_id)
            assert source_class is not None
            assert target_class is not None

            save_seat_preferences(
                session=session,
                discovered_class=source_class,
                included_seats=["33", "35"],
                excluded_seats=[],
                scope_kind=ROOM_CATEGORY_SCOPE_KIND,
            )
            session.commit()

            payload = seat_preferences_payload(session=session, discovered_class=target_class)

        self.assertEqual(["33", "35"], payload["seat_preferences"]["included_seats"])
        self.assertEqual("room_category", payload["seat_preference_source"]["source_kind"])



if __name__ == "__main__":
    unittest.main()
