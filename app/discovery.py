from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
import json
import re
import time as time_module
from typing import Iterable

from playwright.async_api import async_playwright
from sqlmodel import select

from app.adapters.gym_site import ExampleGymAdapter
from app.booking_time import calculate_booking_open
from app.config import get_settings
from app.db import get_session, init_db
from app.gym_auth import auth_state_context_kwargs, ensure_auth_state_directory
from app.models import DiscoveredClass, DiscoveryEvent
from app.time_utils import local_now, local_today
from app.weekly_schedule import materialize_weekly_schedule


@dataclass
class DiscoveryRefreshSummary:
    scanned_ranges: int = 0
    matched_classes: int = 0
    elapsed_seconds: float = 0.0
    messages: list[str] = field(default_factory=list)


@dataclass
class BookingProbeResult:
    status: str
    message: str
    site_booking_opens_at: datetime | None = None
    raw_status_code: int | None = None


def _date_ranges(date_from: date, date_to: date) -> list[tuple[date, date]]:
    cursor = _week_start(date_from)
    ranges = []
    final_week_start = _week_start(date_to)
    while cursor <= final_week_start:
        range_end = cursor + timedelta(days=6)
        ranges.append((cursor, range_end))
        cursor = range_end + timedelta(days=1)
    return ranges


def _week_start(value: date) -> date:
    days_since_sunday = (value.weekday() + 1) % 7
    return value - timedelta(days=days_since_sunday)


def _latest_week_scan_event(
    session,
    range_start: date,
    range_end: date,
) -> DiscoveryEvent | None:
    return session.exec(
        select(DiscoveryEvent).where(
            DiscoveryEvent.event_type == "api_range_scanned",
            DiscoveryEvent.visible_week_start == range_start,
            DiscoveryEvent.visible_week_end == range_end,
        ).order_by(DiscoveryEvent.observed_at.desc())
    ).first()


def _latest_week_refresh_attempt(
    session,
    range_start: date,
    range_end: date,
) -> DiscoveryEvent | None:
    return session.exec(
        select(DiscoveryEvent).where(
            DiscoveryEvent.event_type.in_([
                "api_range_scanned",
                "range_unavailable",
                "view_refresh_requested",
                "view_refresh_failed",
            ]),
            DiscoveryEvent.visible_week_start == range_start,
            DiscoveryEvent.visible_week_end == range_end,
        ).order_by(DiscoveryEvent.observed_at.desc())
    ).first()


def _week_needs_refresh(
    session,
    range_start: date,
    range_end: date,
    refreshed_before: datetime,
) -> bool:
    latest_scan = _latest_week_scan_event(
        session=session,
        range_start=range_start,
        range_end=range_end,
    )
    if latest_scan is None:
        return True
    return latest_scan.observed_at < refreshed_before


def _week_needs_view_refresh(
    session,
    range_start: date,
    range_end: date,
    refreshed_before: datetime,
) -> bool:
    latest_attempt = _latest_week_refresh_attempt(
        session=session,
        range_start=range_start,
        range_end=range_end,
    )
    if latest_attempt is None:
        return True
    return latest_attempt.observed_at < refreshed_before


def _normalize_terms(search_terms: Iterable[str]) -> list[str]:
    return [term.strip() for term in search_terms if term.strip()]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _parse_api_date_time(value: str | None) -> tuple[date | None, datetime_time | None]:
    if not value:
        return None, None
    match = re.match(r"(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})", value)
    if not match:
        return None, None
    return date.fromisoformat(match.group("date")), datetime_time.fromisoformat(match.group("time"))


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _gym_booking_status(event: dict) -> str:
    return str(event.get("BookingStatus") or "").strip()


def _gym_booking_spot_labels(event: dict) -> str:
    return ", ".join(_string_list(event.get("LayoutItemIdTitles")))


def _gym_booking_actions(event: dict) -> str:
    return ", ".join(_string_list(event.get("BottomSheetButtons")))


def _has_booked_member_details(event: dict) -> bool:
    details = event.get("BookedMemberDetails") or {}
    if not isinstance(details, dict):
        return False
    return any(
        bool(details.get(key))
        for key in ["UserId", "FullName", "Email", "PicUrl"]
    )


def _is_booked_at_gym(event: dict) -> bool:
    status = _normalize_text(_gym_booking_status(event))
    booked_statuses = {"booked", "reserved", "registered", "confirmed"}
    not_booked_statuses = {"cancelled", "canceled", "not booked", "none", "waitlist", "waitlisted"}
    if status in booked_statuses or any(token in status for token in booked_statuses):
        return True
    if status in not_booked_statuses:
        return False
    if status and "wait" not in status:
        return True
    if _has_booked_member_details(event):
        return True
    actions = {_normalize_text(action) for action in _string_list(event.get("BottomSheetButtons"))}
    return any(action in actions for action in ["cancel-booking", "cancel", "manage-booking"])


def parse_booking_open_hint(raw_message: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    current_time = now or local_now()
    match = re.search(
        r"can\s+only\s+be\s+booked\s+starting\s+(?P<when>[^.]+)",
        raw_message,
        re.IGNORECASE,
    )
    if not match:
        return None, ""

    when_text = match.group("when").strip()
    normalized = re.sub(r"\s+", " ", when_text).strip()
    normalized = re.sub(r"\bat\b", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    formats = [
        "%A %b %d %I:%M %p",
        "%A, %b %d %I:%M %p",
        "%a %b %d %I:%M %p",
        "%a, %b %d %I:%M %p",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue

        parsed = parsed.replace(year=current_time.year)
        if parsed < current_time - timedelta(days=1):
            parsed = parsed.replace(year=current_time.year + 1)
        return parsed, when_text

    return None, when_text


def _event_instructor_name(event: dict) -> str:
    instructors = event.get("Instructors") or []
    if not isinstance(instructors, list):
        return ""
    names = [
        str(instructor.get("InstructorFullName") or instructor.get("InstructorNickname") or "").strip()
        for instructor in instructors
        if isinstance(instructor, dict)
    ]
    return ", ".join(name for name in names if name)


def _event_search_text(event: dict) -> str:
    values = [
        event.get("EventName"),
        event.get("Location"),
        event.get("BookingTypeCategory"),
        event.get("DepartmentTitle"),
        _event_instructor_name(event),
    ]
    return _normalize_text(" ".join(str(value or "") for value in values))


def _event_matches_terms(event: dict, terms: list[str]) -> bool:
    search_text = _event_search_text(event)
    return any(_normalize_text(term) in search_text for term in terms)


def _format_time(value: datetime_time | None) -> str:
    if value is None:
        return ""
    return datetime.combine(date.today(), value).strftime("%I:%M %p")


def _event_summary(event: dict) -> str:
    _event_date, start_time = _parse_api_date_time(event.get("StartTime") or event.get("StartDate"))
    duration = _int_or_none(event.get("Duration"))
    title = str(event.get("EventName") or "Unknown class").strip()
    instructor_name = _event_instructor_name(event)
    category = str(event.get("Location") or event.get("BookingTypeCategory") or "").strip()
    if _is_booked_at_gym(event):
        action = "Booked"
    elif event.get("IsFull"):
        action = "Book Now (Full)"
    else:
        action = "Book Now"

    lines = [
        _format_time(start_time),
        f"({duration} min)" if duration is not None else "",
        title,
        instructor_name,
        category,
        action,
    ]
    return "\n".join(line for line in lines if line)


def _event_source_key(event: dict, class_date: date | None, start_time: datetime_time | None) -> str:
    calendar_event_id = _int_or_none(event.get("CalendarEventId"))
    if calendar_event_id is not None:
        return f"calendar_event:{calendar_event_id}"

    title = _normalize_text(str(event.get("EventName") or "unknown-class"))
    instructor = _normalize_text(_event_instructor_name(event))
    date_key = class_date.isoformat() if class_date else "unknown-date"
    time_key = start_time.isoformat(timespec="minutes") if start_time else "unknown-time"
    return f"{date_key}|{time_key}|{title}|{instructor}"


def _upsert_api_discovered_class(session, event: dict, observed_at: datetime) -> DiscoveredClass | None:
    class_date, start_time = _parse_api_date_time(event.get("StartTime") or event.get("StartDate"))
    if class_date is None:
        return None

    calendar_event_id = _int_or_none(event.get("CalendarEventId"))
    source_key = _event_source_key(event=event, class_date=class_date, start_time=start_time)
    title = str(event.get("EventName") or "Unknown class").strip()

    discovered_class = None
    if calendar_event_id is not None:
        discovered_class = session.exec(
            select(DiscoveredClass).where(DiscoveredClass.calendar_event_id == calendar_event_id)
        ).first()

    if discovered_class is None:
        discovered_class = session.exec(
            select(DiscoveredClass).where(DiscoveredClass.source_key == source_key)
        ).first()

    if discovered_class is None and start_time is not None:
        discovered_class = session.exec(
            select(DiscoveredClass).where(
                DiscoveredClass.class_date == class_date,
                DiscoveredClass.start_time == start_time,
                DiscoveredClass.title == title,
            )
        ).first()

    if discovered_class is None:
        discovered_class = DiscoveredClass(
            source_key=source_key,
            title=title,
            class_date=class_date,
            start_time=start_time,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )

    discovered_class.source_key = source_key
    discovered_class.calendar_event_id = calendar_event_id
    discovered_class.layout_id = _int_or_none(event.get("LayoutId"))
    discovered_class.title = title
    discovered_class.class_date = class_date
    discovered_class.start_time = start_time
    discovered_class.instructor_name = _event_instructor_name(event)
    discovered_class.category = str(event.get("Location") or event.get("BookingTypeCategory") or "").strip()
    discovered_class.duration_minutes = _int_or_none(event.get("Duration"))
    discovered_class.event_type = str(event.get("EventType") or "")
    discovered_class.booking_type_id = _int_or_none(event.get("BookingTypeId"))
    discovered_class.resource_id = _int_or_none(event.get("ResourceId"))
    discovered_class.can_show_layout = _bool_or_none(event.get("CanShowLayout"))
    discovered_class.seat_title = str(
        event.get("SeatTitle") or event.get("EquipmentName") or ""
    ).strip()
    discovered_class.total_spots = _int_or_none(event.get("TotalSpots"))
    discovered_class.total_available_spots = _int_or_none(event.get("TotalAvailableSpots"))
    discovered_class.total_booked = _int_or_none(event.get("TotalBooked"))
    discovered_class.is_full = _bool_or_none(event.get("IsFull"))
    discovered_class.can_book_event = _bool_or_none(event.get("CanBookEvent"))
    discovered_class.gym_booking_status = _gym_booking_status(event)
    discovered_class.is_booked_at_gym = _is_booked_at_gym(event)
    discovered_class.gym_booking_spot_labels = _gym_booking_spot_labels(event)
    discovered_class.gym_booking_actions = _gym_booking_actions(event)
    discovered_class.deep_link_url = str(event.get("DeepLinkUrl") or "")
    discovered_class.expected_booking_opens_at = calculate_booking_open(class_date)
    discovered_class.raw_summary = _event_summary(event)
    discovered_class.api_payload = json.dumps(event, sort_keys=True, default=str)
    discovered_class.last_seen_at = observed_at

    live_probe_result = _live_booking_probe_result(event)
    if live_probe_result is not None:
        discovered_class.booking_probe_status = live_probe_result.status
        discovered_class.booking_probe_message = live_probe_result.message
        discovered_class.last_probed_at = observed_at
    elif discovered_class.booking_probe_status in {"booked_at_gym", "waitlisted_at_gym"}:
        discovered_class.booking_probe_status = ""
        discovered_class.booking_probe_message = ""
        discovered_class.last_probed_at = None

    session.add(discovered_class)
    session.flush()
    return discovered_class


def _classify_booking_constraint_response(response_payload: dict) -> BookingProbeResult:
    status_code = _int_or_none(response_payload.get("status"))
    body = response_payload.get("body") or {}
    message = str(
        body.get("message")
        or body.get("originalMessage")
        or ("Booking constraint check passed." if response_payload.get("ok") else "Booking constraint check failed.")
    )
    site_booking_opens_at, _raw_when = parse_booking_open_hint(message)

    if site_booking_opens_at is not None:
        return BookingProbeResult(
            status="not_yet_open",
            message=message,
            site_booking_opens_at=site_booking_opens_at,
            raw_status_code=status_code,
        )
    if response_payload.get("ok"):
        return BookingProbeResult(
            status="bookable_now",
            message=message,
            raw_status_code=status_code,
        )
    return BookingProbeResult(
        status="blocked",
        message=message,
        raw_status_code=status_code,
    )


def _find_matching_calendar_event(
    api_events: list[dict],
    discovered_class: DiscoveredClass,
) -> dict | None:
    if discovered_class.calendar_event_id is not None:
        for event in api_events:
            if _int_or_none(event.get("CalendarEventId")) == discovered_class.calendar_event_id:
                return event

    normalized_title = _normalize_text(discovered_class.title)
    for event in api_events:
        event_date, event_time = _parse_api_date_time(event.get("StartTime") or event.get("StartDate"))
        if event_date != discovered_class.class_date:
            continue
        if (
            discovered_class.start_time is not None
            and event_time is not None
            and event_time != discovered_class.start_time
        ):
            continue
        if normalized_title and _normalize_text(str(event.get("EventName") or "")) != normalized_title:
            continue
        return event

    return None


def _live_booking_probe_result(event: dict) -> BookingProbeResult | None:
    raw_status = _gym_booking_status(event)
    normalized_status = _normalize_text(raw_status)
    actions = _gym_booking_actions(event)
    spot_labels = _gym_booking_spot_labels(event)

    if _is_booked_at_gym(event):
        details = []
        if raw_status:
            details.append(f"status '{raw_status}'")
        if spot_labels:
            details.append(f"spot labels {spot_labels}")
        if actions:
            details.append(f"actions {actions}")
        suffix = f" Live signal: {', '.join(details)}." if details else ""
        return BookingProbeResult(
            status="booked_at_gym",
            message=f"Live calendar state shows this class is already booked at the gym.{suffix}",
        )

    if normalized_status in {"waitlist", "waitlisted"} or "wait" in normalized_status:
        details = []
        if raw_status:
            details.append(f"status '{raw_status}'")
        if actions:
            details.append(f"actions {actions}")
        suffix = f" Live signal: {', '.join(details)}." if details else ""
        return BookingProbeResult(
            status="waitlisted_at_gym",
            message=f"Live calendar state shows this class is already waitlisted at the gym.{suffix}",
        )

    return None


async def probe_discovered_class_booking_status_async(
    discovered_class_id: int,
    headless: bool = True,
) -> DiscoveredClass:
    init_db()
    auth_state_path = ensure_auth_state_directory()

    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise ValueError("Discovered class not found")
        calendar_event_id = discovered_class.calendar_event_id
        event_type = discovered_class.event_type or "in-person"
        class_date = discovered_class.class_date

    if calendar_event_id is None:
        raise ValueError("Discovered class does not have a calendar_event_id. Refresh discovery first.")

    adapter = ExampleGymAdapter()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(**auth_state_context_kwargs())
        page = None
        try:
            await adapter.ensure_logged_in(context)
            page = await adapter.open_calendar_page(context=context)
            response_payload = await adapter.check_booking_constraint(
                page=page,
                calendar_event_id=calendar_event_id,
                calendar_event_type=event_type,
            )
            probe_result = _classify_booking_constraint_response(response_payload)
            week_start = _week_start(class_date)
            week_end = week_start + timedelta(days=6)
            matching_event = None
            try:
                api_events = await adapter.fetch_calendar_events(
                    page=page,
                    start_date=week_start,
                    end_date=week_end,
                )
            except Exception:
                api_events = []
            if api_events:
                probe_class = DiscoveredClass(
                    source_key="probe",
                    calendar_event_id=calendar_event_id,
                    title=discovered_class.title,
                    class_date=class_date,
                    start_time=discovered_class.start_time,
                )
                matching_event = _find_matching_calendar_event(
                    api_events=api_events,
                    discovered_class=probe_class,
                )
                live_result = _live_booking_probe_result(matching_event) if matching_event is not None else None
                if live_result is not None:
                    probe_result = live_result
            await context.storage_state(path=str(auth_state_path))
        finally:
            if page is not None and not page.is_closed():
                await page.close()
            await context.close()
            await browser.close()

    observed_at = local_now()
    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise ValueError("Discovered class not found after probe")

        if matching_event is not None:
            refreshed_class = _upsert_api_discovered_class(
                session=session,
                event=matching_event,
                observed_at=observed_at,
            )
            if refreshed_class is not None:
                discovered_class = refreshed_class

        discovered_class.booking_probe_status = probe_result.status
        discovered_class.booking_probe_message = probe_result.message
        discovered_class.site_booking_opens_at = probe_result.site_booking_opens_at
        discovered_class.last_probed_at = observed_at
        session.add(discovered_class)
        session.add(
            DiscoveryEvent(
                discovered_class_id=discovered_class.id,
                event_type=f"booking_probe_{probe_result.status}",
                message=probe_result.message,
                visible_week_start=discovered_class.class_date,
                visible_week_end=discovered_class.class_date,
                observed_at=observed_at,
            )
        )
        session.commit()
        session.refresh(discovered_class)
        return discovered_class


def probe_discovered_class_booking_status(discovered_class_id: int, headless: bool = True) -> DiscoveredClass:
    return asyncio.run(
        probe_discovered_class_booking_status_async(
            discovered_class_id=discovered_class_id,
            headless=headless,
        )
    )


async def refresh_discovery_async(
    search_terms: Iterable[str] | None = None,
    days_ahead: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    stale_only: bool = False,
) -> DiscoveryRefreshSummary:
    init_db()
    settings = get_settings()
    terms = _normalize_terms(search_terms or settings.discovery_terms)
    lookahead_days = days_ahead if days_ahead is not None else settings.discovery_days_ahead
    summary = DiscoveryRefreshSummary()
    refresh_started_at = time_module.perf_counter()

    if not terms:
        summary.messages.append("No discovery class types are configured.")
        return summary

    auth_state_path = ensure_auth_state_directory()

    today = local_today()
    start_date = date_from or today
    end_date = date_to or (start_date + timedelta(days=lookahead_days))
    if end_date < start_date:
        raise ValueError("date_to must be on or after date_from.")

    refresh_cutoff = local_now() - timedelta(days=settings.discovery_week_stale_days)
    adapter = ExampleGymAdapter()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context(**auth_state_context_kwargs())
        page = None
        try:
            await adapter.ensure_logged_in(context)
            page = await adapter.open_calendar_page(context=context)
            for range_start, range_end in _date_ranges(date_from=start_date, date_to=end_date):
                week_started_at = time_module.perf_counter()
                if stale_only:
                    with get_session() as session:
                        if not _week_needs_refresh(
                            session=session,
                            range_start=range_start,
                            range_end=range_end,
                            refreshed_before=refresh_cutoff,
                        ):
                            continue

                if range_end < start_date or range_start > end_date:
                    continue
                summary.scanned_ranges += 1

                try:
                    api_events = await adapter.fetch_calendar_events(
                        page=page,
                        start_date=range_start,
                        end_date=range_end,
                    )
                except Exception as exc:
                    observed_at = local_now()
                    with get_session() as session:
                        session.add(
                            DiscoveryEvent(
                                event_type="range_unavailable",
                                message=(
                                    f"Could not fetch calendar API range "
                                    f"{range_start.isoformat()} through {range_end.isoformat()}: {exc}"
                                ),
                                visible_week_start=range_start,
                                visible_week_end=range_end,
                                observed_at=observed_at,
                            )
                        )
                        session.commit()
                    summary.messages.append(
                        f"Could not fetch {range_start.isoformat()} to {range_end.isoformat()}: {exc}"
                    )
                    continue

                observed_at = local_now()
                week_match_count = 0

                with get_session() as session:
                    session.add(
                        DiscoveryEvent(
                            event_type="api_range_scanned",
                            message=(
                                f"Fetched {range_start.isoformat()} through "
                                f"{range_end.isoformat()} for: {', '.join(terms)}."
                            ),
                            visible_week_start=range_start,
                            visible_week_end=range_end,
                            observed_at=observed_at,
                        )
                    )

                    for event in api_events:
                        class_date, _start_time = _parse_api_date_time(
                            event.get("StartTime") or event.get("StartDate")
                        )
                        if class_date is None:
                            continue
                        if class_date < start_date or class_date > end_date:
                            continue
                        if not _event_matches_terms(event, terms):
                            continue

                        discovered_class = _upsert_api_discovered_class(
                            session=session,
                            event=event,
                            observed_at=observed_at,
                        )
                        if discovered_class is None:
                            continue

                        week_match_count += 1
                        summary.matched_classes += 1
                        session.add(
                            DiscoveryEvent(
                                discovered_class_id=discovered_class.id,
                                event_type="matched_class",
                                message="Discovered class: " + discovered_class.raw_summary.replace("\n", " | "),
                                visible_week_start=range_start,
                                visible_week_end=range_end,
                                observed_at=observed_at,
                            )
                        )

                    session.commit()

                week_elapsed_seconds = time_module.perf_counter() - week_started_at
                summary.messages.append(
                    f"Fetched {range_start.isoformat()} to {range_end.isoformat()}: "
                    f"{week_match_count} matching class(es) in {week_elapsed_seconds:.1f}s."
                )
        finally:
            await context.storage_state(path=str(auth_state_path))
            if page is not None and not page.is_closed():
                await page.close()
            await context.close()
            await browser.close()

    materialized = materialize_weekly_schedule()
    if materialized["created_count"]:
        summary.messages.append(
            "Weekly schedule selected "
            f"{materialized['created_count']} upcoming class(es)."
        )

    summary.elapsed_seconds = time_module.perf_counter() - refresh_started_at
    return summary


def refresh_discovery(
    search_terms: Iterable[str] | None = None,
    days_ahead: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    stale_only: bool = False,
) -> DiscoveryRefreshSummary:
    return asyncio.run(
        refresh_discovery_async(
            search_terms=search_terms,
            days_ahead=days_ahead,
            date_from=date_from,
            date_to=date_to,
            stale_only=stale_only,
        )
    )
