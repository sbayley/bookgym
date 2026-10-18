from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app.adapters.gym_site import ExampleGymAdapter
from app.db import get_session, init_db
from app.gym_auth import auth_state_context_kwargs, ensure_auth_state_directory
from app.models import DiscoveredClass
from app.seat_preferences import seat_preferences_payload


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


def discovered_class_seat_metadata(discovered_class: DiscoveredClass) -> dict[str, object]:
    layout_id = discovered_class.layout_id
    can_show_layout = discovered_class.can_show_layout
    seat_title = discovered_class.seat_title.strip()

    if discovered_class.api_payload:
        try:
            api_payload = json.loads(discovered_class.api_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            api_payload = {}
        if isinstance(api_payload, dict):
            if layout_id is None:
                layout_id = _int_or_none(api_payload.get("LayoutId"))
            if can_show_layout is None:
                can_show_layout = _bool_or_none(api_payload.get("CanShowLayout"))
            if not seat_title:
                seat_title = str(
                    api_payload.get("SeatTitle") or api_payload.get("EquipmentName") or ""
                ).strip()

    if not seat_title:
        seat_title = "Spot"

    has_seat_map = layout_id is not None and can_show_layout is not False
    return {
        "layout_id": layout_id,
        "can_show_layout": can_show_layout,
        "seat_title": seat_title,
        "has_seat_map": has_seat_map,
    }


def _icon_name_from_url(image_url: str | None) -> str:
    if not image_url:
        return ""
    path = urlsplit(image_url).path
    return Path(path).stem.lower()


def _cell_kind(cell: dict) -> str:
    if cell.get("IsBooked"):
        return "booked"
    if cell.get("IsUnavailable"):
        return "unavailable"
    if cell.get("CanBook"):
        return "available"

    icon_name = _icon_name_from_url(cell.get("ImageUrlOfBooked"))
    if icon_name == "door":
        return "door"
    if icon_name == "trainer":
        return "trainer"
    return "walkway"


def _display_title(cell: dict, kind: str) -> str:
    title = str(cell.get("Title") or "").strip()
    if kind in {"door", "trainer", "walkway"}:
        return ""
    return title


def _normalize_booking_layout(body: dict) -> dict[str, object]:
    rows = _int_or_none(body.get("rows")) or 0
    columns = _int_or_none(body.get("columns")) or 0
    raw_cells = body.get("data") or []
    cells: list[dict[str, object]] = []
    summary = {
        "available": 0,
        "booked": 0,
        "unavailable": 0,
        "non_bookable": 0,
    }

    if not isinstance(raw_cells, list):
        raw_cells = []

    for raw_cell in raw_cells:
        if not isinstance(raw_cell, dict):
            continue

        kind = _cell_kind(raw_cell)
        title = str(raw_cell.get("Title") or "").strip()
        display_title = _display_title(raw_cell, kind)

        normalized_cell = {
            "row": _int_or_none(raw_cell.get("Row")),
            "column": _int_or_none(raw_cell.get("Column")),
            "seat_id": str(raw_cell.get("SeatId") or ""),
            "title": title,
            "display_title": display_title,
            "can_book": bool(raw_cell.get("CanBook")),
            "is_booked": bool(raw_cell.get("IsBooked")),
            "is_unavailable": bool(raw_cell.get("IsUnavailable")),
            "max_guests": _int_or_none(raw_cell.get("MaxGuests")),
            "image_url": str(raw_cell.get("ImageUrlOfBooked") or ""),
            "icon_name": _icon_name_from_url(raw_cell.get("ImageUrlOfBooked")),
            "kind": kind,
        }
        cells.append(normalized_cell)

        if kind == "available":
            summary["available"] += 1
        elif kind == "booked":
            summary["booked"] += 1
        elif kind == "unavailable":
            summary["unavailable"] += 1
        else:
            summary["non_bookable"] += 1

    return {
        "rows": rows,
        "columns": columns,
        "cells": cells,
        "summary": summary,
    }


async def fetch_discovered_class_seat_layout_async(
    discovered_class_id: int,
    headless: bool = True,
    user_id: int | None = None,
) -> dict[str, object]:
    init_db()
    auth_state_path = ensure_auth_state_directory()

    with get_session() as session:
        discovered_class = session.get(DiscoveredClass, discovered_class_id)
        if discovered_class is None:
            raise ValueError("Discovered class not found")
        metadata = discovered_class_seat_metadata(discovered_class)
        preferences = seat_preferences_payload(
            session=session,
            discovered_class=discovered_class,
            user_id=user_id,
        )
        calendar_event_id = discovered_class.calendar_event_id
        layout_id = metadata["layout_id"]
        seat_title = str(metadata["seat_title"])
        has_seat_map = bool(metadata["has_seat_map"])

        if calendar_event_id is None:
            raise ValueError("Discovered class does not have a calendar_event_id. Refresh discovery first.")
        if not has_seat_map or layout_id is None:
            raise ValueError("This class does not expose a seat layout.")

        class_payload = {
            "class_id": discovered_class.id,
            "calendar_event_id": calendar_event_id,
            "layout_id": layout_id,
            "title": discovered_class.title,
            "class_date": discovered_class.class_date.isoformat(),
            "start_time": (
                discovered_class.start_time.isoformat(timespec="seconds")
                if discovered_class.start_time is not None
                else None
            ),
            "seat_title": seat_title,
        }

    adapter = ExampleGymAdapter()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(**auth_state_context_kwargs())
        page = None
        try:
            await adapter.ensure_logged_in(context)
            page = await adapter.open_calendar_page(context=context)
            layout_payload = await adapter.fetch_booking_layout(
                page=page,
                calendar_event_id=calendar_event_id,
                layout_id=layout_id,
            )
            await context.storage_state(path=str(auth_state_path))
        finally:
            if page is not None and not page.is_closed():
                await page.close()
            await context.close()
            await browser.close()

    normalized_layout = _normalize_booking_layout(
        body=layout_payload,
    )
    return {
        **class_payload,
        **preferences,
        **normalized_layout,
    }


def fetch_discovered_class_seat_layout(
    discovered_class_id: int,
    headless: bool = True,
    user_id: int | None = None,
) -> dict[str, object]:
    return asyncio.run(
        fetch_discovered_class_seat_layout_async(
            discovered_class_id=discovered_class_id,
            headless=headless,
            user_id=user_id,
        )
    )
