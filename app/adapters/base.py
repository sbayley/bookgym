from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from playwright.async_api import BrowserContext


@dataclass
class BookingResult:
    status: str
    message: str
    screenshot_path: str = ""
    selected_seat_label: str = ""
    available_at_hint: datetime | None = None
    available_at_hint_exact: bool = False
    matched_class_title: str = ""
    matched_class_date: date | None = None
    matched_class_time: time | None = None
    matched_class_summary: str = ""
    matched_calendar_event_id: int | None = None
    network_artifact_path: str = ""


class GymSiteAdapter:
    async def ensure_logged_in(self, context: BrowserContext) -> None:
        raise NotImplementedError

    async def book_class(
        self,
        context: BrowserContext,
        class_name: str,
        class_date: date,
        window_start_time: time | None = None,
        window_end_time: time | None = None,
        seat_labels_to_try: list[str] | None = None,
        seat_selection_source: str | None = None,
        require_preferred_seat: bool = False,
        select_any_available_seat: bool = False,
        allow_any_available_fallback: bool = False,
        calendar_event_id: int | None = None,
        dry_run: bool = False,
    ) -> BookingResult:
        raise NotImplementedError
