from __future__ import annotations

from dataclasses import dataclass
import json
import re

from sqlmodel import select

from app.config import get_settings
from app.models import DiscoveredClass, SeatPreference
from app.time_utils import local_now


@dataclass(frozen=True)
class SeatSelectionPlan:
    seat_labels: list[str]
    source: str
    hard_constraint: bool
    select_any_available_seat: bool = False
    allow_any_available_fallback: bool = False


ROOM_CATEGORY_SCOPE_KIND = "room_category"
CLASS_SLOT_SCOPE_KIND = "class_slot"
SELECTION_MODE_PREFERENCES = "preferences"
SELECTION_MODE_STRICT = "strict"
SELECTION_MODE_ANY = "any"

VALID_SCOPE_KINDS = {ROOM_CATEGORY_SCOPE_KIND, CLASS_SLOT_SCOPE_KIND}
VALID_SELECTION_MODES = {SELECTION_MODE_PREFERENCES, SELECTION_MODE_STRICT, SELECTION_MODE_ANY}


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_seat_preference_category(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _normalize_scope_token(value: str) -> str:
    normalized = normalize_seat_preference_category(value)
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def seat_preference_category_label(discovered_class: DiscoveredClass) -> str:
    category_label = str(discovered_class.category or "").strip()
    if category_label:
        return category_label
    title = str(discovered_class.title or "").strip()
    return title or "Class"


def _class_slot_label(discovered_class: DiscoveredClass) -> str:
    title = str(discovered_class.title or "").strip() or seat_preference_category_label(discovered_class)
    weekday_label = discovered_class.class_date.strftime("%A")
    if discovered_class.start_time is None:
        return f"{title} on {weekday_label}s"
    time_label = discovered_class.start_time.strftime("%I:%M %p").lstrip("0")
    return f"{title} on {weekday_label}s at {time_label}"


def discovered_class_seat_layout_id(discovered_class: DiscoveredClass) -> int | None:
    layout_id = discovered_class.layout_id
    if layout_id is not None:
        return layout_id

    if not discovered_class.api_payload:
        return None

    try:
        api_payload = json.loads(discovered_class.api_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        api_payload = {}

    if not isinstance(api_payload, dict):
        return None

    return _int_or_none(api_payload.get("LayoutId"))


def discovered_class_seat_preference_scope(
    discovered_class: DiscoveredClass,
    scope_kind: str = ROOM_CATEGORY_SCOPE_KIND,
) -> dict[str, object]:
    if scope_kind not in VALID_SCOPE_KINDS:
        raise ValueError(f"Unknown seat preference scope: {scope_kind}")

    layout_id = discovered_class_seat_layout_id(discovered_class)
    category_label = seat_preference_category_label(discovered_class)
    category_key = normalize_seat_preference_category(category_label)
    has_scope = layout_id is not None and bool(category_key)

    if scope_kind == CLASS_SLOT_SCOPE_KIND:
        title_key = _normalize_scope_token(discovered_class.title)
        weekday = discovered_class.class_date.weekday()
        start_time_key = (
            discovered_class.start_time.isoformat(timespec="minutes")
            if discovered_class.start_time is not None
            else ""
        )
        has_class_scope = has_scope and bool(title_key) and bool(start_time_key)
        scope_key = (
            f"class-slot:{layout_id}:{category_key}:{weekday}:{start_time_key}:{title_key}"
            if has_class_scope
            else ""
        )
        return {
            "scope_key": scope_key,
            "scope_kind": CLASS_SLOT_SCOPE_KIND,
            "layout_id": layout_id,
            "category_key": category_key,
            "category_label": _class_slot_label(discovered_class),
            "base_category_label": category_label,
            "title_key": title_key,
            "weekday": weekday,
            "weekday_label": discovered_class.class_date.strftime("%A"),
            "start_time": start_time_key,
            "has_scope": has_class_scope,
        }

    scope_key = f"{layout_id}:{category_key}" if has_scope else ""
    return {
        "scope_key": scope_key,
        "scope_kind": ROOM_CATEGORY_SCOPE_KIND,
        "layout_id": layout_id,
        "category_key": category_key,
        "category_label": category_label,
        "has_scope": has_scope,
    }


def seat_label_sort_key(value: str) -> tuple[int, int | str, str]:
    label = str(value or "").strip()
    if label.isdigit():
        return (0, int(label), label)
    return (1, label.lower(), label)


def ordered_seat_labels(values: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    ordered_labels: list[str] = []
    unique_labels: set[str] = set()
    for value in values or []:
        label = str(value or "").strip()
        if not label or label in unique_labels:
            continue
        unique_labels.add(label)
        ordered_labels.append(label)
    return ordered_labels


def sanitize_seat_labels(values: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    return sorted(ordered_seat_labels(values), key=seat_label_sort_key)


def configured_default_seat_labels() -> list[str]:
    ordered_labels: list[str] = []
    seen_labels: set[str] = set()
    for seat_number in get_settings().preferred_seats:
        label = str(seat_number).strip()
        if not label or label in seen_labels:
            continue
        seen_labels.add(label)
        ordered_labels.append(label)
    return ordered_labels


def _load_seat_labels(raw_value: str, preserve_order: bool = False) -> list[str]:
    try:
        parsed = json.loads(raw_value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    seat_labels = [str(item) for item in parsed]
    if preserve_order:
        return ordered_seat_labels(seat_labels)
    return sanitize_seat_labels(seat_labels)


def _dump_seat_labels(values: list[str], preserve_order: bool = False) -> str:
    if preserve_order:
        return json.dumps(ordered_seat_labels(values))
    return json.dumps(sanitize_seat_labels(values))


def normalize_selection_mode(value: str | None) -> str:
    mode = str(value or SELECTION_MODE_PREFERENCES).strip().lower()
    if mode not in VALID_SELECTION_MODES:
        raise ValueError(f"Unknown seat selection mode: {value}")
    return mode


def load_scoped_seat_preference(
    session,
    discovered_class: DiscoveredClass,
    scope_kind: str = ROOM_CATEGORY_SCOPE_KIND,
    user_id: int | None = None,
) -> SeatPreference | None:
    scope = discovered_class_seat_preference_scope(discovered_class, scope_kind=scope_kind)
    if not scope["has_scope"]:
        return None
    statement = select(SeatPreference).where(SeatPreference.scope_key == str(scope["scope_key"]))
    if user_id is not None:
        statement = statement.where(SeatPreference.user_id == user_id)
    return session.exec(statement).first()


def _seat_preferences_from_preference(
    preference: SeatPreference | None,
    *,
    selection_mode: str | None = None,
    included_seats: list[str] | None = None,
    excluded_seats: list[str] | None = None,
    has_saved_preferences: bool | None = None,
) -> dict[str, object]:
    normalized_included = (
        _load_seat_labels(preference.included_seats_json, preserve_order=True)
        if preference is not None
        else ordered_seat_labels(included_seats)
        if included_seats is not None
        else []
    )
    normalized_excluded = (
        _load_seat_labels(preference.excluded_seats_json)
        if preference is not None
        else sanitize_seat_labels(excluded_seats)
        if excluded_seats is not None
        else []
    )
    normalized_selection_mode = (
        normalize_selection_mode(preference.selection_mode)
        if preference is not None
        else normalize_selection_mode(selection_mode)
    )
    saved = (
        bool(
            normalized_included
            or normalized_excluded
            or normalized_selection_mode != SELECTION_MODE_PREFERENCES
        )
        if has_saved_preferences is None
        else has_saved_preferences
    )
    return {
        "included_seats": normalized_included,
        "excluded_seats": normalized_excluded,
        "selection_mode": normalized_selection_mode,
        "has_saved_preferences": saved,
    }


def _seat_preference_signature(source: dict[str, object]) -> str:
    return json.dumps(
        {
            "included": list(source["included_seats"]),
            "excluded": list(source["excluded_seats"]),
            "selection_mode": str(source["selection_mode"]),
        },
        sort_keys=True,
    )


def _class_slot_parts_from_scope_key(scope_key: str) -> dict[str, object] | None:
    match = re.fullmatch(
        r"class-slot:(?P<layout_id>\d+):(?P<category_key>[^:]+):(?P<weekday>\d+):(?P<start_time>\d{2}:\d{2}):(?P<title_key>.+)",
        scope_key,
    )
    if not match:
        return None
    return {
        "layout_id": int(match.group("layout_id")),
        "category_key": match.group("category_key"),
        "weekday": int(match.group("weekday")),
        "start_time": match.group("start_time"),
        "title_key": match.group("title_key"),
    }


def _source_from_preference(
    preference: SeatPreference,
    *,
    source_type: str,
    source_kind: str,
    label: str | None = None,
    relevance: int = 0,
    exact_match: bool = False,
) -> dict[str, object]:
    payload = _seat_preferences_from_preference(preference, has_saved_preferences=True)
    return {
        **payload,
        "source_key": preference.scope_key,
        "source_type": source_type,
        "source_kind": source_kind,
        "scope_key": preference.scope_key,
        "label": label or preference.category_label or preference.scope_key,
        "relevance": relevance,
        "usage_count": 1,
        "updated_at": preference.updated_at.isoformat(),
        "exact_match": exact_match,
    }


def _default_seat_preference_source(default_seats: list[str]) -> dict[str, object]:
    return {
        **_seat_preferences_from_preference(
            None,
            selection_mode=SELECTION_MODE_PREFERENCES,
            included_seats=default_seats,
            excluded_seats=[],
            has_saved_preferences=False,
        ),
        "source_key": "default",
        "source_type": "default",
        "source_kind": "default",
        "scope_key": "",
        "label": "Default seat list",
        "relevance": 99,
        "usage_count": 0,
        "updated_at": "",
        "exact_match": False,
    }


def _related_seat_preference_sources(
    session,
    discovered_class: DiscoveredClass,
    user_id: int | None = None,
) -> list[dict[str, object]]:
    class_scope = discovered_class_seat_preference_scope(
        discovered_class,
        scope_kind=CLASS_SLOT_SCOPE_KIND,
    )
    room_scope = discovered_class_seat_preference_scope(discovered_class)
    layout_id = class_scope["layout_id"]
    category_key = str(class_scope["category_key"])
    title_key = str(class_scope.get("title_key") or "")
    exact_scope_key = str(class_scope["scope_key"])
    room_scope_key = str(room_scope["scope_key"])

    sources: list[dict[str, object]] = []
    if layout_id is not None:
        statement = select(SeatPreference).where(SeatPreference.layout_id == int(layout_id))
        if user_id is not None:
            statement = statement.where(SeatPreference.user_id == user_id)
        preferences = session.exec(statement).all()
        for preference in preferences:
            if preference.scope_key == exact_scope_key:
                continue

            if preference.scope_key == room_scope_key:
                sources.append(
                    _source_from_preference(
                        preference,
                        source_type="inferred",
                        source_kind=ROOM_CATEGORY_SCOPE_KIND,
                        label=f"Common {room_scope['category_label']} room prefs",
                        relevance=20,
                    )
                )
                continue

            parts = _class_slot_parts_from_scope_key(preference.scope_key)
            if parts is None or parts["category_key"] != category_key:
                continue

            same_title = parts["title_key"] == title_key
            relevance = 0 if same_title else 10
            if normalize_selection_mode(preference.selection_mode) == SELECTION_MODE_ANY:
                relevance = 120
            sources.append(
                _source_from_preference(
                    preference,
                    source_type="inferred",
                    source_kind=CLASS_SLOT_SCOPE_KIND,
                    relevance=relevance,
                )
            )

    saved_sources = sources[:]
    usage_counts: dict[str, int] = {}
    for source in saved_sources:
        signature = _seat_preference_signature(source)
        usage_counts[signature] = usage_counts.get(signature, 0) + 1
    for source in saved_sources:
        source["usage_count"] = usage_counts.get(_seat_preference_signature(source), 1)

    return sorted(
        [*saved_sources, _default_seat_preference_source(configured_default_seat_labels())],
        key=lambda source: (
            int(source["relevance"]),
            -int(source["usage_count"]),
            str(source["label"]).lower(),
        ),
    )


def _active_seat_preference_source(
    session,
    discovered_class: DiscoveredClass,
    user_id: int | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    class_preference = load_scoped_seat_preference(
        session=session,
        discovered_class=discovered_class,
        scope_kind=CLASS_SLOT_SCOPE_KIND,
        user_id=user_id,
    )
    related_sources = _related_seat_preference_sources(
        session=session,
        discovered_class=discovered_class,
        user_id=user_id,
    )
    if class_preference is not None:
        return (
            _source_from_preference(
                class_preference,
                source_type="explicit",
                source_kind=CLASS_SLOT_SCOPE_KIND,
                label="Saved for this class slot",
                exact_match=True,
            ),
            related_sources,
        )
    return related_sources[0], related_sources


def seat_preferences_payload(
    session,
    discovered_class: DiscoveredClass,
    user_id: int | None = None,
) -> dict[str, object]:
    class_scope = discovered_class_seat_preference_scope(
        discovered_class,
        scope_kind=CLASS_SLOT_SCOPE_KIND,
    )
    room_scope = discovered_class_seat_preference_scope(discovered_class)
    class_preference = load_scoped_seat_preference(
        session=session,
        discovered_class=discovered_class,
        scope_kind=CLASS_SLOT_SCOPE_KIND,
        user_id=user_id,
    )
    room_preference = load_scoped_seat_preference(
        session=session,
        discovered_class=discovered_class,
        user_id=user_id,
    )
    active_source, related_sources = _active_seat_preference_source(
        session=session,
        discovered_class=discovered_class,
        user_id=user_id,
    )
    return {
        "preference_scope": class_scope,
        "seat_preferences": _seat_preferences_from_preference(
            None,
            selection_mode=str(active_source["selection_mode"]),
            included_seats=list(active_source["included_seats"]),
            excluded_seats=list(active_source["excluded_seats"]),
            has_saved_preferences=active_source["source_type"] == "explicit",
        ),
        "seat_preference_source": {
            key: value
            for key, value in active_source.items()
            if key not in ["included_seats", "excluded_seats"]
        },
        "seat_preference_sources": related_sources,
        "class_slot_preference_scope": class_scope,
        "class_slot_seat_preferences": _seat_preferences_from_preference(
            class_preference,
            has_saved_preferences=class_preference is not None,
        ),
        "room_category_preference_scope": room_scope,
        "room_category_seat_preferences": _seat_preferences_from_preference(room_preference),
    }


def save_seat_preferences(
    session,
    discovered_class: DiscoveredClass,
    included_seats: list[str] | None,
    excluded_seats: list[str] | None,
    scope_kind: str = CLASS_SLOT_SCOPE_KIND,
    selection_mode: str = "",
    user_id: int | None = None,
) -> dict[str, object]:
    scope = discovered_class_seat_preference_scope(discovered_class, scope_kind=scope_kind)
    if not scope["has_scope"]:
        raise ValueError("This class does not have a seat-preference scope yet.")

    requested_selection_mode = str(selection_mode or "").strip().lower()
    requested_included = ordered_seat_labels(included_seats)
    requested_excluded = sanitize_seat_labels(excluded_seats)
    if requested_selection_mode:
        normalized_selection_mode = normalize_selection_mode(requested_selection_mode)
    elif requested_included:
        normalized_selection_mode = SELECTION_MODE_STRICT
    elif requested_excluded:
        normalized_selection_mode = SELECTION_MODE_PREFERENCES
    else:
        normalized_selection_mode = SELECTION_MODE_ANY

    normalized_included = [] if normalized_selection_mode == SELECTION_MODE_ANY else requested_included
    normalized_excluded = [] if normalized_selection_mode == SELECTION_MODE_ANY else requested_excluded
    if (
        scope_kind == CLASS_SLOT_SCOPE_KIND
        and normalized_selection_mode == SELECTION_MODE_PREFERENCES
        and not normalized_included
        and not normalized_excluded
    ):
        normalized_selection_mode = SELECTION_MODE_ANY
    if normalized_selection_mode == SELECTION_MODE_STRICT and not normalized_included:
        raise ValueError("Only-these seat mode needs at least one preferred seat.")

    overlap = sorted(
        set(normalized_included).intersection(normalized_excluded),
        key=seat_label_sort_key,
    )
    if overlap:
        joined = ", ".join(overlap)
        raise ValueError(f"A seat cannot be both included and excluded: {joined}")

    preference = load_scoped_seat_preference(
        session=session,
        discovered_class=discovered_class,
        scope_kind=scope_kind,
        user_id=user_id,
    )
    if (
        scope_kind == ROOM_CATEGORY_SCOPE_KIND
        and normalized_selection_mode in {SELECTION_MODE_PREFERENCES, SELECTION_MODE_ANY}
        and not normalized_included
        and not normalized_excluded
    ):
        if preference is not None:
            session.delete(preference)
            session.flush()
        return seat_preferences_payload(
            session=session,
            discovered_class=discovered_class,
            user_id=user_id,
        )

    now = local_now()
    if preference is None:
        preference = SeatPreference(
            user_id=user_id,
            scope_key=str(scope["scope_key"]),
            layout_id=int(scope["layout_id"]),
            category_key=str(scope["category_key"]),
            category_label=str(scope["category_label"]),
            selection_mode=normalized_selection_mode,
            created_at=now,
            updated_at=now,
        )
    else:
        if user_id is not None:
            preference.user_id = user_id
        preference.layout_id = int(scope["layout_id"])
        preference.category_key = str(scope["category_key"])
        preference.category_label = str(scope["category_label"])
        preference.selection_mode = normalized_selection_mode
        preference.updated_at = now

    preference.included_seats_json = _dump_seat_labels(normalized_included, preserve_order=True)
    preference.excluded_seats_json = _dump_seat_labels(normalized_excluded)
    if discovered_class.layout_id is None and scope["layout_id"] is not None:
        discovered_class.layout_id = int(scope["layout_id"])
        session.add(discovered_class)
    session.add(preference)
    session.flush()
    return seat_preferences_payload(
        session=session,
        discovered_class=discovered_class,
        user_id=user_id,
    )


def reset_class_slot_seat_preferences(
    session,
    discovered_class: DiscoveredClass,
    user_id: int | None = None,
) -> dict[str, object]:
    preference = load_scoped_seat_preference(
        session=session,
        discovered_class=discovered_class,
        scope_kind=CLASS_SLOT_SCOPE_KIND,
        user_id=user_id,
    )
    if preference is not None:
        session.delete(preference)
        session.flush()
    return seat_preferences_payload(
        session=session,
        discovered_class=discovered_class,
        user_id=user_id,
    )


def resolve_booking_seat_selection(
    session,
    discovered_class: DiscoveredClass | None,
    user_id: int | None = None,
) -> SeatSelectionPlan:
    default_seats = configured_default_seat_labels()
    has_default_seats = bool(default_seats)
    if discovered_class is None:
        return SeatSelectionPlan(
            seat_labels=default_seats,
            source="default preferred seat list",
            hard_constraint=has_default_seats,
            allow_any_available_fallback=not has_default_seats,
        )

    active_source, _related_sources = _active_seat_preference_source(
        session=session,
        discovered_class=discovered_class,
        user_id=user_id,
    )
    if active_source["source_type"] == "default":
        return SeatSelectionPlan(
            seat_labels=default_seats,
            source=str(active_source["label"]),
            hard_constraint=has_default_seats,
            allow_any_available_fallback=not has_default_seats,
        )
    if active_source["selection_mode"] == SELECTION_MODE_ANY:
        return SeatSelectionPlan(
            seat_labels=[],
            source=str(active_source["label"]),
            hard_constraint=False,
            select_any_available_seat=True,
        )

    included_seats = ordered_seat_labels(list(active_source["included_seats"]))
    excluded_seats = set(sanitize_seat_labels(list(active_source["excluded_seats"])))
    if included_seats:
        is_strict = active_source["selection_mode"] == SELECTION_MODE_STRICT
        return SeatSelectionPlan(
            seat_labels=[seat for seat in included_seats if seat not in excluded_seats],
            source=str(active_source["label"]),
            hard_constraint=is_strict,
            allow_any_available_fallback=not is_strict,
        )
    if excluded_seats:
        return SeatSelectionPlan(
            seat_labels=[seat for seat in default_seats if seat not in excluded_seats],
            source=str(active_source["label"]),
            hard_constraint=False,
        )

    return SeatSelectionPlan(
        seat_labels=default_seats,
        source=str(active_source["label"]),
        hard_constraint=has_default_seats,
        allow_any_available_fallback=not has_default_seats,
    )
