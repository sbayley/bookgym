from __future__ import annotations

from dataclasses import dataclass
import re
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path

from playwright.async_api import BrowserContext, Locator, Page

from app.adapters.base import BookingResult, GymSiteAdapter
from app.config import get_settings
from app.gym_auth import record_login_result, resolve_gym_credentials


@dataclass
class CalendarHeader:
    text: str
    column_center_x: float
    slot_date: date


@dataclass
class CalendarCardCandidate:
    locator: Locator
    slot_date: date | None
    slot_time: time | None
    summary: str
    calendar_event_id: int | None = None


@dataclass
class CalendarCardSnapshot:
    slot_date: date | None
    slot_time: time | None
    summary: str


@dataclass
class BookingGateHint:
    message: str
    available_at: datetime | None = None
    is_exact_time: bool = False


def _time_in_window(candidate: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= candidate <= end
    return candidate >= start or candidate <= end


class ExampleGymAdapter(GymSiteAdapter):
    calendar_url_suffix = "section=Calendar"

    async def ensure_logged_in(self, context: BrowserContext) -> None:
        settings = get_settings()
        page = await context.new_page()
        try:
            await page.goto(settings.login_url)
            await page.wait_for_load_state("networkidle")

            if await self._is_login_page(page):
                credentials = resolve_gym_credentials()
                if credentials is None:
                    raise RuntimeError(
                        "Stored browser session is not logged in, and no saved gym credentials are available. "
                        "Add credentials in BookGym or run python scripts/login_once.py and sign in again."
                    )

                try:
                    await self._login_with_saved_credentials(
                        page=page,
                        username=credentials.username,
                        password=credentials.password,
                    )
                except RuntimeError as exc:
                    record_login_result(False, str(exc))
                    raise

                record_login_result(True)
        finally:
            await page.close()

    async def _is_login_page(self, page: Page) -> bool:
        if "login" in page.url.lower():
            return True
        password_locator = page.locator("input[type='password']")
        return await password_locator.count() > 0 and await password_locator.first.is_visible()

    async def _login_with_saved_credentials(self, page: Page, username: str, password: str) -> None:
        username_locator = await self._first_visible_locator(
            page=page,
            selectors=[
                "input[type='email']",
                "input[name*='email' i]",
                "input[id*='email' i]",
                "input[autocomplete='username']",
                "input[name*='user' i]",
                "input[id*='user' i]",
                "input[autocomplete='on'][type='text']",
                "input[type='text']",
            ],
        )
        password_locator = await self._first_visible_locator(
            page=page,
            selectors=[
                "input[type='password']",
                "input[autocomplete='current-password']",
                "input[name*='password' i]",
                "input[id*='password' i]",
            ],
        )
        if username_locator is None or password_locator is None:
            raise RuntimeError(
                "BookGym could not find the gym login fields. The login page may have changed."
            )

        await username_locator.fill(username)
        await password_locator.fill(password)

        submit_locator = await self._first_visible_locator(
            page=page,
            selectors=[
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "button:has-text('Login')",
                "[role='button']:has-text('Sign in')",
                "[role='button']:has-text('Log in')",
                "[role='button']:has-text('Login')",
            ],
        )
        if submit_locator is not None:
            await submit_locator.click()
        else:
            await password_locator.press("Enter")

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1_000)

        if await self._is_login_page(page):
            raise RuntimeError(
                "BookGym could not log into the gym site with the saved credentials."
            )

    async def _first_visible_locator(
        self,
        page: Page,
        selectors: list[str],
    ) -> Locator | None:
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        return None

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
        if get_settings().preview_mode:
            return BookingResult(
                status="canceled",
                message=(
                    f"Preview mode blocked the booking attempt for '{class_name}' on "
                    f"{class_date.isoformat()}. No gym page was opened."
                ),
                matched_class_title=class_name,
                matched_class_date=class_date,
            )

        artifacts_dir = Path("artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        page = await self.open_calendar_page(context=context)

        candidate = await self._find_booking_candidate(
            page=page,
            class_name=class_name,
            class_date=class_date,
            window_start_time=window_start_time,
            window_end_time=window_end_time,
            calendar_event_id=calendar_event_id,
        )
        if candidate is None:
            screenshot_path = artifacts_dir / f"booking-not-found-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            await page.close()
            message = f"Could not find an available '{class_name}' class on the current calendar view."
            if window_start_time and window_end_time:
                message = (
                    f"Could not find an available '{class_name}' class for "
                    f"{class_date.isoformat()} "
                    f"{window_start_time.isoformat(timespec='minutes')}-"
                    f"{window_end_time.isoformat(timespec='minutes')}."
                )
            return BookingResult(
                status="failed",
                message=message,
                screenshot_path=str(screenshot_path),
            )

        result = await self.book_candidate(
            page=page,
            candidate=candidate,
            class_name=class_name,
            class_date=class_date,
            seat_labels_to_try=seat_labels_to_try,
            seat_selection_source=seat_selection_source,
            require_preferred_seat=require_preferred_seat,
            select_any_available_seat=select_any_available_seat,
            allow_any_available_fallback=allow_any_available_fallback,
            dry_run=dry_run,
        )
        await page.close()
        return result

    async def open_calendar_page(self, context: BrowserContext) -> Page:
        settings = get_settings()
        page = await context.new_page()
        await page.goto(settings.calendar_url)
        await page.wait_for_load_state("networkidle")
        return page

    async def open_calendar_week_containing(
        self,
        context: BrowserContext,
        target_date: date,
        max_week_clicks: int = 12,
    ) -> tuple[Page, list[CalendarHeader]]:
        page = await self.open_calendar_page(context=context)
        headers = await self._navigate_calendar_to_week(
            page=page,
            target_date=target_date,
            max_week_clicks=max_week_clicks,
        )
        return page, headers

    async def fetch_calendar_events(
        self,
        page: Page,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        settings = get_settings()
        payload = await page.evaluate(
            """async ({ department, endDate, fitnessCenterId, startDate }) => {
                const token = window.localStorage.getItem("activeLoginId") || "";
                const headers = {
                    accept: "application/json, text/plain, */*",
                    authorization: `Bearer ${token}`,
                    "x-device": "angular",
                    "x-device-api-version": "77",
                };
                const params = new URLSearchParams({
                    fitnessCenterIds: String(fitnessCenterId),
                    type: "byDate",
                    trainerId: "",
                    bookingType: "classes",
                    bookingTypeId: "",
                    startDate,
                    endDate,
                });
                const response = await fetch(
                    `/v2/calendar/${department}/events?${params.toString()}`,
                    { credentials: "include", headers }
                );
                const text = await response.text();
                let body = null;
                try {
                    body = JSON.parse(text);
                } catch (error) {
                    body = { parseError: String(error), text: text.slice(0, 1000) };
                }
                return {
                    ok: response.ok,
                    status: response.status,
                    body,
                };
            }""",
            {
                "department": settings.department,
                "endDate": end_date.isoformat(),
                "fitnessCenterId": settings.fitness_center_id,
                "startDate": start_date.isoformat(),
            },
        )
        if not payload["ok"]:
            raise RuntimeError(
                "Calendar events API request failed "
                f"with status {payload['status']}: {payload['body']}"
            )
        body = payload["body"] or {}
        events = body.get("events") or []
        if not isinstance(events, list):
            return []
        return events

    async def check_booking_constraint(
        self,
        page: Page,
        calendar_event_id: int,
        calendar_event_type: str = "in-person",
        required_session_balance: int = 1,
    ) -> dict:
        settings = get_settings()
        return await page.evaluate(
            """async ({ calendarEventId, calendarEventType, department, fitnessCenterId, requiredSessionBalance }) => {
                const token = window.localStorage.getItem("activeLoginId") || "";
                const headers = {
                    accept: "application/json, text/plain, */*",
                    authorization: `Bearer ${token}`,
                    "x-device": "angular",
                    "x-device-api-version": "77",
                };
                const profileResponse = await fetch("/v2/profile", { credentials: "include", headers });
                const profileText = await profileResponse.text();
                let profile = null;
                try {
                    profile = JSON.parse(profileText);
                } catch (error) {
                    return {
                        ok: false,
                        status: profileResponse.status,
                        body: {
                            message: "Could not read profile JSON before booking constraint check.",
                            raw: profileText.slice(0, 1000),
                        },
                    };
                }
                const userId = profile && profile.profile && profile.profile.UserId;
                if (!userId) {
                    return {
                        ok: false,
                        status: profileResponse.status,
                        body: { message: "Could not determine booking user id from profile." },
                    };
                }

                const params = new URLSearchParams({
                    calendarEventType,
                    fitnessCenterId: String(fitnessCenterId),
                    department,
                    requiredSessionBalance: String(requiredSessionBalance),
                    isMemberGuest: "false",
                    bookingUserId: String(userId),
                });
                const response = await fetch(
                    `/v2/calendar/${calendarEventId}/checkBookingConstraintAndBalance/${userId}?${params.toString()}`,
                    { credentials: "include", headers }
                );
                const text = await response.text();
                let body = null;
                try {
                    body = JSON.parse(text);
                } catch (error) {
                    body = { message: text.slice(0, 1000) };
                }
                return {
                    ok: response.ok,
                    status: response.status,
                    body,
                };
            }""",
            {
                "calendarEventId": calendar_event_id,
                "calendarEventType": calendar_event_type or "in-person",
                "department": settings.department,
                "fitnessCenterId": settings.fitness_center_id,
                "requiredSessionBalance": required_session_balance,
            },
        )

    async def fetch_booking_layout(
        self,
        page: Page,
        calendar_event_id: int,
        layout_id: int,
    ) -> dict:
        payload = await page.evaluate(
            """async ({ calendarEventId, layoutId }) => {
                const token = window.localStorage.getItem("activeLoginId") || "";
                const headers = {
                    accept: "application/json, text/plain, */*",
                    authorization: `Bearer ${token}`,
                    "x-device": "angular",
                    "x-device-api-version": "77",
                };
                const response = await fetch(
                    `/v2/calendar/${calendarEventId}/bookingLayout/${layoutId}`,
                    { credentials: "include", headers }
                );
                const text = await response.text();
                let body = null;
                try {
                    body = JSON.parse(text);
                } catch (error) {
                    body = { message: text.slice(0, 1000) };
                }
                return {
                    ok: response.ok,
                    status: response.status,
                    body,
                };
            }""",
            {
                "calendarEventId": calendar_event_id,
                "layoutId": layout_id,
            },
        )
        if not payload["ok"]:
            raise RuntimeError(
                "Booking layout API request failed "
                f"with status {payload['status']}: {payload['body']}"
            )
        body = payload["body"] or {}
        if not isinstance(body, dict):
            raise RuntimeError("Booking layout API returned an unexpected payload.")
        return body

    async def _navigate_calendar_to_week(
        self,
        page: Page,
        target_date: date,
        max_week_clicks: int = 12,
    ) -> list[CalendarHeader]:
        previous_ranges: set[tuple[date, date]] = set()

        for _ in range(max_week_clicks + 1):
            headers = await self._extract_calendar_headers(page=page, target_year=target_date.year)
            visible_dates = sorted(header.slot_date for header in headers)
            if not visible_dates:
                return headers
            if visible_dates[0] <= target_date <= visible_dates[-1]:
                return headers

            visible_range = (visible_dates[0], visible_dates[-1])
            if visible_range in previous_ranges:
                return headers
            previous_ranges.add(visible_range)

            direction = "next" if target_date > visible_dates[-1] else "previous"
            clicked = await self._click_calendar_week_arrow(page=page, direction=direction)
            if not clicked:
                return headers

            await page.wait_for_timeout(750)
            await page.wait_for_load_state("networkidle")

        return await self._extract_calendar_headers(page=page, target_year=target_date.year)

    async def _click_calendar_week_arrow(self, page: Page, direction: str) -> bool:
        target_label = "next" if direction == "next" else "previous"
        selectors = [
            f"button[aria-label*='{target_label}' i]",
            f"[role='button'][aria-label*='{target_label}' i]",
            f"button[title*='{target_label}' i]",
            f"[role='button'][title*='{target_label}' i]",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count():
                button = locator.first
                if await button.is_visible():
                    await button.click()
                    return True

        icon_text = "chevron_right" if direction == "next" else "chevron_left"
        icon = page.get_by_text(icon_text, exact=True)
        if await icon.count():
            button = icon.first.locator(
                "xpath=ancestor::*[self::button or @role='button' or contains(@class, 'mat-button')][1]"
            )
            if await button.count() and await button.first.is_visible():
                await button.first.click()
                return True

        arrow = "›" if direction == "next" else "‹"
        arrow_candidates = page.get_by_text(re.compile(rf"^\s*{re.escape(arrow)}\s*$"), exact=False)
        count = await arrow_candidates.count()
        for index in range(count):
            candidate = arrow_candidates.nth(index)
            if not await candidate.is_visible():
                continue
            box = await candidate.bounding_box()
            if box is None:
                continue
            if box["y"] > 500:
                continue
            await candidate.click()
            return True

        return await page.evaluate(
            """(direction) => {
                const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 8 &&
                        rect.height > 8 &&
                        rect.y < 500 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none"
                    );
                };
                const isNext = direction === "next";
                const candidates = Array.from(document.querySelectorAll("button, [role='button'], a"))
                    .filter((el) => visible(el))
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const label = normalize(
                            el.getAttribute("aria-label") ||
                            el.getAttribute("title") ||
                            el.textContent ||
                            ""
                        );
                        return { el, rect, label };
                    })
                    .filter((item) => {
                        if (isNext && /next|right|chevron_right|›|>/i.test(item.label)) {
                            return true;
                        }
                        if (!isNext && /previous|prev|left|chevron_left|‹|</i.test(item.label)) {
                            return true;
                        }
                        return false;
                    })
                    .sort((a, b) => isNext ? b.rect.x - a.rect.x : a.rect.x - b.rect.x);

                if (!candidates.length) {
                    return false;
                }
                candidates[0].el.click();
                return true;
            }""",
            direction,
        )

    async def book_candidate(
        self,
        page: Page,
        candidate: CalendarCardCandidate,
        class_name: str,
        class_date: date,
        seat_labels_to_try: list[str] | None = None,
        seat_selection_source: str | None = None,
        require_preferred_seat: bool = False,
        select_any_available_seat: bool = False,
        allow_any_available_fallback: bool = False,
        dry_run: bool = False,
    ) -> BookingResult:
        settings = get_settings()
        if settings.preview_mode:
            return BookingResult(
                status="canceled",
                message=(
                    f"Preview mode blocked the booking attempt for '{class_name}' on "
                    f"{class_date.isoformat()}. No gym action was clicked."
                ),
                matched_class_title=self._extract_candidate_title(candidate.summary),
                matched_class_date=candidate.slot_date or class_date,
                matched_class_time=candidate.slot_time,
                matched_class_summary=candidate.summary,
                matched_calendar_event_id=getattr(candidate, "calendar_event_id", None),
            )

        artifacts_dir = Path("artifacts")
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        if self.calendar_url_suffix not in page.url:
            screenshot_path = artifacts_dir / f"booking-navigation-error-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=f"Did not reach the expected calendar page. Current URL: {page.url}",
                screenshot_path=str(screenshot_path),
            )

        card = candidate.locator
        class_summary = candidate.summary.replace("\n", " | ")
        matched_class_kwargs = {
            "matched_class_title": self._extract_candidate_title(candidate.summary),
            "matched_class_date": candidate.slot_date or class_date,
            "matched_class_time": candidate.slot_time,
            "matched_class_summary": candidate.summary,
            "matched_calendar_event_id": getattr(candidate, "calendar_event_id", None),
        }
        book_button = await self._find_clickable_action(card=card, label="Book Now")
        if book_button is None:
            screenshot_path = artifacts_dir / f"booking-action-missing-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=f"Found '{class_name}' on the calendar, but no clickable 'Book Now' action was available.",
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )

        await book_button.click()
        details_modal = await self._wait_for_active_modal(page=page, text_hint=class_name)
        details_state, book_a_spot_button = await self._wait_for_booking_details_state(
            page=page,
            card=details_modal,
            timeout_ms=15_000,
        )
        if details_state == "already_booked":
            screenshot_path = artifacts_dir / f"booking-already-booked-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="booked",
                message=(
                    f"{class_name} was already booked at the gym; the class details showed "
                    "'Cancel Booking'. No additional booking was attempted."
                ),
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )
        if book_a_spot_button is None:
            screenshot_path = artifacts_dir / f"booking-details-modal-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=(
                    "Booking could not continue because the class details showed neither "
                    "'Book a Spot' nor 'Cancel Booking'."
                ),
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )

        await book_a_spot_button.click()
        post_booking_state, post_booking_value = await self._wait_for_post_booking_state(
            page=page,
            timeout_ms=10_000,
        )
        not_yet_open_hint = (
            post_booking_value if post_booking_state == "not_yet_open" else None
        )
        if not_yet_open_hint is not None:
            assert isinstance(not_yet_open_hint, BookingGateHint)
            screenshot_path = artifacts_dir / f"booking-not-yet-open-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="not_yet_open",
                message=f"{not_yet_open_hint.message} Match used: {class_summary}",
                screenshot_path=str(screenshot_path),
                available_at_hint=not_yet_open_hint.available_at,
                available_at_hint_exact=not_yet_open_hint.is_exact_time,
                **matched_class_kwargs,
            )
        non_cancelable_confirmation = (
            str(post_booking_value) if post_booking_state == "needs_confirmation" else None
        )
        if non_cancelable_confirmation is not None:
            screenshot_path = artifacts_dir / f"booking-needs-confirmation-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="needs_confirmation",
                message=(
                    "The site showed a non-cancelable booking confirmation and no confirmation "
                    f"button was clicked. Site message: '{non_cancelable_confirmation}'. "
                    f"Match used: {class_summary}"
                ),
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )
        immediate_success_confirmation = (
            str(post_booking_value) if post_booking_state == "booked" else None
        )
        if immediate_success_confirmation is not None:
            screenshot_path = artifacts_dir / f"booking-success-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="booked",
                message=f"{class_name} was booked successfully. No seat selection was required.",
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )
        if post_booking_state != "seat_selection" or post_booking_value is None:
            screenshot_path = artifacts_dir / f"booking-seat-selection-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=(
                    "The gym did not show a booking confirmation or seat-selection prompt. "
                    f"Match used: {class_summary}"
                ),
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )
        seat_modal = post_booking_value
        seat_map_ready = await self._wait_for_seat_map_ready(page=page, seat_modal=seat_modal)
        not_yet_open_hint = await self._detect_not_yet_open(page=page)
        if not_yet_open_hint is not None:
            screenshot_path = artifacts_dir / f"booking-not-yet-open-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="not_yet_open",
                message=f"{not_yet_open_hint.message} Match used: {class_summary}",
                screenshot_path=str(screenshot_path),
                available_at_hint=not_yet_open_hint.available_at,
                available_at_hint_exact=not_yet_open_hint.is_exact_time,
                **matched_class_kwargs,
            )
        if not seat_map_ready:
            delayed_success_confirmation = await self._detect_booking_success_confirmation(page=page)
            if delayed_success_confirmation is not None:
                screenshot_path = artifacts_dir / f"booking-success-{class_date.isoformat()}.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                return BookingResult(
                    status="booked",
                    message=f"{class_name} was booked successfully. No seat selection was required.",
                    screenshot_path=str(screenshot_path),
                    **matched_class_kwargs,
                )
            screenshot_path = artifacts_dir / f"booking-seat-selection-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=(
                    f"The seat-selection modal did not finish loading. Match used: {class_summary}"
                ),
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )
        configured_seat_labels = [str(seat_number) for seat_number in settings.preferred_seats]
        target_seat_labels = [label.strip() for label in (seat_labels_to_try or configured_seat_labels) if label and label.strip()]
        selection_source = seat_selection_source or "default preferred seat list"
        used_any_available_fallback = False
        if select_any_available_seat:
            selected_seat_label = await self._select_any_available_seat(
                page=page,
                seat_modal=seat_modal,
            )
        else:
            selected_seat_label = await self._select_seat_from_candidates(
                page=page,
                seat_modal=seat_modal,
                seat_labels=target_seat_labels,
            )
            if selected_seat_label is None and allow_any_available_fallback:
                selected_seat_label = await self._select_any_available_seat(
                    page=page,
                    seat_modal=seat_modal,
                )
                used_any_available_fallback = selected_seat_label is not None

        if used_any_available_fallback:
            selection_source = (
                f"{selection_source}; preferred seats unavailable, fell back to any available seat"
            )

        if selected_seat_label is None:
            screenshot_path = artifacts_dir / f"booking-seat-selection-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            if select_any_available_seat:
                message = (
                    "The seat-selection modal opened, but I could not select any available seat. "
                    f"Strategy used: {selection_source}. Match used: {class_summary}"
                )
            elif allow_any_available_fallback:
                message = (
                    "The seat-selection modal opened, but I could not select any preferred seat from "
                    f"{target_seat_labels} or any fallback seat. Strategy used: {selection_source}. "
                    f"Match used: {class_summary}"
                )
            elif not target_seat_labels:
                message = (
                    "The seat-selection modal opened, but there were no eligible seat labels to try. "
                    f"Strategy used: {selection_source}. Match used: {class_summary}"
                )
            elif require_preferred_seat:
                message = (
                    "The seat-selection modal opened, but I could not select any included seat from "
                    f"{target_seat_labels}. Strategy used: {selection_source}. Match used: {class_summary}"
                )
            else:
                message = (
                    "The seat-selection modal opened, but I could not select any seat from "
                    f"{target_seat_labels}. Strategy used: {selection_source}. Match used: {class_summary}"
                )
            return BookingResult(
                status="failed",
                message=message,
                screenshot_path=str(screenshot_path),
                **matched_class_kwargs,
            )

        if require_preferred_seat and selected_seat_label not in set(target_seat_labels):
            screenshot_path = artifacts_dir / f"booking-seat-selection-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=(
                    f"Selected seat {selected_seat_label}, but strict mode only allows "
                    f"{target_seat_labels}. Stopped before confirming the booking. "
                    f"Strategy used: {selection_source}. Match used: {class_summary}"
                ),
                screenshot_path=str(screenshot_path),
                selected_seat_label=selected_seat_label,
                **matched_class_kwargs,
            )

        done_button = await self._find_clickable_action(card=seat_modal, label="Done")
        if done_button is None:
            screenshot_path = artifacts_dir / f"booking-done-missing-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="failed",
                message=(
                    f"Selected seat {selected_seat_label}, but could not find the 'Done' button. "
                    f"Strategy used: {selection_source}. Match used: {class_summary}"
                ),
                screenshot_path=str(screenshot_path),
                selected_seat_label=selected_seat_label,
                **matched_class_kwargs,
            )

        if dry_run:
            screenshot_path = artifacts_dir / f"booking-dry-run-{class_date.isoformat()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return BookingResult(
                status="dry_run",
                message=(
                    f"Dry run found '{class_name}' and selected seat {selected_seat_label}. "
                    f"It did not click 'Done'. Strategy used: {selection_source}. Match used: {class_summary}"
                ),
                screenshot_path=str(screenshot_path),
                selected_seat_label=selected_seat_label,
                **matched_class_kwargs,
            )

        await done_button.click()
        booking_confirmed = await self._wait_for_success_signal(page=page, timeout_ms=5_000)

        screenshot_path = artifacts_dir / f"booking-attempt-{class_date.isoformat()}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        message = await self._build_result_message(
            page=page,
            class_name=class_name,
            seat_label=selected_seat_label,
        )
        status = "booked" if booking_confirmed else "failed"
        return BookingResult(
            status=status,
            message=message,
            screenshot_path=str(screenshot_path),
            selected_seat_label=selected_seat_label,
            **matched_class_kwargs,
        )

    def _extract_candidate_title(self, summary: str) -> str:
        lines = [line.strip() for line in summary.splitlines() if line.strip()]
        if len(lines) >= 3:
            return lines[2]
        return lines[0] if lines else ""

    async def _find_booking_candidate(
        self,
        page: Page,
        class_name: str,
        class_date: date,
        window_start_time: time | None,
        window_end_time: time | None,
        calendar_event_id: int | None = None,
    ) -> CalendarCardCandidate | None:
        search_terms = [class_name]
        target_event = None
        if calendar_event_id is not None:
            target_event = await self._find_calendar_event_by_id(
                page=page,
                class_date=class_date,
                calendar_event_id=calendar_event_id,
            )
            if target_event is None:
                return None
            event_title = self._calendar_event_title(target_event)
            if event_title:
                search_terms = [event_title, class_name]

        headers = await self._extract_calendar_headers(page=page, target_year=class_date.year)
        candidates = await self._find_candidate_cards(
            page=page,
            search_terms=search_terms,
            headers=headers,
        )

        filtered_candidates: list[CalendarCardCandidate] = []
        requires_exact_slot = window_start_time is not None and window_end_time is not None
        for candidate in candidates:
            if target_event is not None:
                if not self._candidate_matches_calendar_event(candidate, target_event):
                    continue
                candidate.calendar_event_id = calendar_event_id
            if requires_exact_slot:
                if candidate.slot_time is None:
                    continue
                if not _time_in_window(candidate.slot_time, window_start_time, window_end_time):
                    continue
                if candidate.slot_date is None:
                    continue
                if candidate.slot_date != class_date:
                    continue
            filtered_candidates.append(candidate)

        if filtered_candidates:
            return sorted(
                filtered_candidates,
                key=lambda candidate: (
                    candidate.slot_date or class_date,
                    candidate.slot_time or time.min,
                ),
            )[0]

        if requires_exact_slot:
            return None

        if calendar_event_id is not None:
            return None

        if candidates:
            return candidates[0]

        return None

    async def _find_calendar_event_by_id(
        self,
        page: Page,
        class_date: date,
        calendar_event_id: int,
    ) -> dict | None:
        events = await self.fetch_calendar_events(
            page=page,
            start_date=class_date,
            end_date=class_date,
        )
        for event in events:
            if self._calendar_event_id(event) == calendar_event_id:
                return event
        return None

    def _candidate_matches_calendar_event(
        self,
        candidate: CalendarCardCandidate,
        event: dict,
    ) -> bool:
        event_date, event_time = self._calendar_event_date_time(event)
        if event_date is not None and candidate.slot_date != event_date:
            return False
        if event_time is not None and candidate.slot_time != event_time:
            return False

        event_title = self._calendar_event_title(event)
        if event_title:
            candidate_title = self._extract_candidate_title(candidate.summary)
            if self._normalize_text(candidate_title) != self._normalize_text(event_title):
                return False

        return True

    def _calendar_event_id(self, event: dict) -> int | None:
        value = event.get("CalendarEventId")
        if value is None:
            value = event.get("calendarEventId")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _calendar_event_title(self, event: dict) -> str:
        return str(event.get("EventName") or event.get("eventName") or "").strip()

    def _calendar_event_date_time(self, event: dict) -> tuple[date | None, time | None]:
        value = (
            event.get("StartTime")
            or event.get("StartDate")
            or event.get("startTime")
            or event.get("startDate")
        )
        if not value:
            return None, None
        match = re.match(r"(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})", str(value))
        if not match:
            return None, None
        return date.fromisoformat(match.group("date")), time.fromisoformat(match.group("time"))

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    async def _find_candidate_cards(
        self,
        page: Page,
        search_terms: list[str],
        headers: list[CalendarHeader],
        available_only: bool = True,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[CalendarCardCandidate]:
        results: list[CalendarCardCandidate] = []
        seen_keys: set[str] = set()
        candidate_xpath = "xpath=ancestor::*[.//*[contains(normalize-space(.), 'Book Now')]"
        if available_only:
            candidate_xpath += " and not(.//*[contains(normalize-space(.), 'Book Now (Full)')])"
        candidate_xpath += "]"

        for scroll_y in await self._calendar_scroll_positions(page):
            await self._scroll_calendar(page=page, scroll_y=scroll_y)
            await page.wait_for_timeout(250)

            for search_term in search_terms:
                text_matches = page.get_by_text(re.compile(re.escape(search_term), re.IGNORECASE), exact=False)
                count = await text_matches.count()
                for index in range(count):
                    text_match = text_matches.nth(index)
                    if not await text_match.is_visible():
                        continue

                    candidate_cards = text_match.locator(candidate_xpath)
                    card_count = await candidate_cards.count()

                    for card_index in range(card_count):
                        card = candidate_cards.nth(card_index)
                        if not await card.is_visible():
                            continue

                        box = await card.bounding_box()
                        if box is None:
                            continue

                        slot_date = self._resolve_slot_date(
                            headers=headers,
                            center_x=box["x"] + (box["width"] / 2),
                        )
                        if (date_from is not None or date_to is not None) and slot_date is None:
                            continue
                        if date_from is not None and slot_date is not None and slot_date < date_from:
                            continue
                        if date_to is not None and slot_date is not None and slot_date > date_to:
                            continue

                        summary = (await card.inner_text()).strip()
                        normalized_summary = summary.lower()
                        if available_only and "book now (full)" in normalized_summary:
                            continue
                        if "book now" not in normalized_summary:
                            continue
                        if normalized_summary.count("book now") != 1:
                            continue
                        if search_term.strip().lower() not in normalized_summary:
                            continue

                        slot_time = self._parse_slot_time(summary)
                        dedupe_key = f"{summary}|{slot_date}|{slot_time}"
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)

                        results.append(
                            CalendarCardCandidate(
                                locator=card,
                                slot_date=slot_date,
                                slot_time=slot_time,
                                summary=summary,
                            )
                        )

        await page.evaluate("() => window.scrollTo(0, 0)")

        return results

    async def extract_calendar_snapshots(
        self,
        page: Page,
        search_terms: list[str],
        headers: list[CalendarHeader],
        available_only: bool = False,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[CalendarCardSnapshot]:
        raw_snapshots = await page.evaluate(
            """(payload) => {
                const normalizeLine = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const normalizeText = (value) => (
                    value || ""
                )
                    .replace(/\\r/g, "\\n")
                    .split("\\n")
                    .map(normalizeLine)
                    .filter(Boolean)
                    .join("\\n");
                const layoutVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 20 &&
                        rect.height > 20 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none" &&
                        style.opacity !== "0"
                    );
                };
                const resolveSlotDate = (centerX) => {
                    if (!payload.headers.length) {
                        return null;
                    }
                    let best = payload.headers[0];
                    let bestDistance = Math.abs(centerX - best.columnCenterX);
                    for (const header of payload.headers.slice(1)) {
                        const distance = Math.abs(centerX - header.columnCenterX);
                        if (distance < bestDistance) {
                            best = header;
                            bestDistance = distance;
                        }
                    }
                    return best.slotDate;
                };
                const inDateRange = (slotDate) => {
                    if (!slotDate && (payload.dateFrom || payload.dateTo)) {
                        return false;
                    }
                    if (payload.dateFrom && slotDate < payload.dateFrom) {
                        return false;
                    }
                    if (payload.dateTo && slotDate > payload.dateTo) {
                        return false;
                    }
                    return true;
                };

                const searchTerms = payload.searchTerms
                    .map((term) => term.trim().toLowerCase())
                    .filter(Boolean);
                const timePattern = /^\\d{1,2}:\\d{2}\\s*[AP]M\\b/i;
                const bookNowPattern = /book\\s+now(?:\\s*\\(full\\))?/gi;
                const snapshotsByKey = new Map();

                for (const el of Array.from(document.querySelectorAll("body *"))) {
                    if (!layoutVisible(el)) {
                        continue;
                    }

                    const summary = normalizeText(el.innerText || el.textContent || "");
                    const lines = summary.split("\\n").filter(Boolean);
                    if (!lines.length || !timePattern.test(lines[0])) {
                        continue;
                    }
                    if (lines.length > 10) {
                        continue;
                    }

                    const bookNowMatches = summary.match(bookNowPattern) || [];
                    if (bookNowMatches.length !== 1) {
                        continue;
                    }
                    if (payload.availableOnly && /book\\s+now\\s*\\(full\\)/i.test(summary)) {
                        continue;
                    }

                    const loweredSummary = summary.toLowerCase();
                    if (!searchTerms.some((term) => loweredSummary.includes(term))) {
                        continue;
                    }

                    const rect = el.getBoundingClientRect();
                    const slotDate = resolveSlotDate(rect.x + (rect.width / 2));
                    if (!inDateRange(slotDate)) {
                        continue;
                    }

                    const key = `${slotDate || "unknown"}|${loweredSummary}`;
                    const value = {
                        summary,
                        slotDate,
                        area: rect.width * rect.height,
                    };
                    const existing = snapshotsByKey.get(key);
                    if (!existing || value.area < existing.area) {
                        snapshotsByKey.set(key, value);
                    }
                }

                return Array.from(snapshotsByKey.values())
                    .sort((a, b) => {
                        if ((a.slotDate || "") !== (b.slotDate || "")) {
                            return (a.slotDate || "").localeCompare(b.slotDate || "");
                        }
                        return a.summary.localeCompare(b.summary);
                    })
                    .map(({ summary, slotDate }) => ({ summary, slotDate }));
            }""",
            {
                "availableOnly": available_only,
                "dateFrom": date_from.isoformat() if date_from else None,
                "dateTo": date_to.isoformat() if date_to else None,
                "headers": [
                    {
                        "columnCenterX": header.column_center_x,
                        "slotDate": header.slot_date.isoformat(),
                    }
                    for header in headers
                ],
                "searchTerms": search_terms,
            },
        )

        snapshots: list[CalendarCardSnapshot] = []
        seen_keys: set[str] = set()
        for raw_snapshot in raw_snapshots:
            summary = raw_snapshot["summary"]
            slot_date = (
                date.fromisoformat(raw_snapshot["slotDate"])
                if raw_snapshot.get("slotDate")
                else None
            )
            slot_time = self._parse_slot_time(summary)
            dedupe_key = f"{slot_date}|{slot_time}|{summary.lower()}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            snapshots.append(
                CalendarCardSnapshot(
                    slot_date=slot_date,
                    slot_time=slot_time,
                    summary=summary,
                )
            )

        return snapshots


    async def _calendar_scroll_positions(self, page: Page) -> list[int]:
        metrics = await page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 150 &&
                        rect.height > 150 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none"
                    );
                };
                const candidates = Array.from(document.querySelectorAll("*"))
                    .map((el) => {
                        if (!visible(el)) {
                            return null;
                        }
                        const scrollHeight = el.scrollHeight || 0;
                        const clientHeight = el.clientHeight || 0;
                        if (scrollHeight <= clientHeight + 100) {
                            return null;
                        }
                        const style = window.getComputedStyle(el);
                        if (!/(auto|scroll)/i.test(style.overflowY)) {
                            return null;
                        }
                        return {
                            scrollHeight,
                            clientHeight,
                        };
                    })
                    .filter(Boolean);

                const maxElementScrollHeight = candidates.length
                    ? Math.max(...candidates.map((item) => item.scrollHeight))
                    : 0;
                const maxElementClientHeight = candidates.length
                    ? Math.max(...candidates.map((item) => item.clientHeight))
                    : 0;

                return {
                    scrollHeight: Math.max(document.documentElement.scrollHeight, maxElementScrollHeight),
                    clientHeight: Math.max(window.innerHeight, maxElementClientHeight),
                };
            }"""
        )
        scroll_height = int(metrics["scrollHeight"])
        inner_height = max(int(metrics["clientHeight"]), 1)

        if scroll_height <= inner_height:
            return [0]

        step = max(int(inner_height * 0.75), 200)
        positions = list(range(0, max(scroll_height - inner_height, 0) + 1, step))
        final_position = max(scroll_height - inner_height, 0)
        if not positions or positions[-1] != final_position:
            positions.append(final_position)
        return positions

    async def _scroll_calendar(self, page: Page, scroll_y: int) -> None:
        await page.evaluate(
            """(scrollY) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 150 &&
                        rect.height > 150 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none"
                    );
                };
                const candidates = Array.from(document.querySelectorAll("*"))
                    .map((el, index) => {
                        if (!visible(el)) {
                            return null;
                        }
                        const scrollHeight = el.scrollHeight || 0;
                        const clientHeight = el.clientHeight || 0;
                        if (scrollHeight <= clientHeight + 100) {
                            return null;
                        }
                        const style = window.getComputedStyle(el);
                        if (!/(auto|scroll)/i.test(style.overflowY)) {
                            return null;
                        }
                        const rect = el.getBoundingClientRect();
                        return {
                            el,
                            area: rect.width * rect.height,
                        };
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.area - a.area);

                for (const candidate of candidates.slice(0, 5)) {
                    candidate.el.scrollTop = scrollY;
                }

                window.scrollTo(0, scrollY);
            }""",
            scroll_y,
        )

    async def _extract_calendar_headers(self, page: Page, target_year: int) -> list[CalendarHeader]:
        raw_headers = await page.evaluate(
            """() => {
                const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 10 &&
                        rect.height > 10 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none"
                    );
                };
                const weekdayPattern = /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)$/;
                const monthDayPattern = /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d{1,2}$/;

                const elements = Array.from(document.querySelectorAll("*"))
                    .filter((el) => visible(el))
                    .map((el) => {
                        const text = normalize(el.textContent || "");
                        const rect = el.getBoundingClientRect();
                        return {
                            text,
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height,
                        };
                    });

                const combined = elements
                    .map((item) => {
                        const match = item.text.match(/(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d{1,2}/);
                        if (!match) {
                            return null;
                        }
                        return {
                            text: match[0],
                            x: item.x,
                            y: item.y,
                            width: item.width,
                        };
                    })
                    .filter(Boolean);

                const weekdays = elements.filter((item) => weekdayPattern.test(item.text));
                const monthDays = elements.filter((item) => monthDayPattern.test(item.text));

                const paired = weekdays
                    .map((weekday) => {
                        const weekdayCenter = weekday.x + (weekday.width / 2);
                        const candidates = monthDays
                            .map((monthDay) => {
                                const monthDayCenter = monthDay.x + (monthDay.width / 2);
                                return {
                                    monthDay,
                                    deltaX: Math.abs(monthDayCenter - weekdayCenter),
                                    deltaY: monthDay.y - weekday.y,
                                };
                            })
                            .filter((item) => item.deltaX <= 80 && item.deltaY >= -10 && item.deltaY <= 80)
                            .sort((a, b) => (a.deltaX + Math.abs(a.deltaY)) - (b.deltaX + Math.abs(b.deltaY)));

                        if (!candidates.length) {
                            return null;
                        }

                        return {
                            text: `${weekday.text} ${candidates[0].monthDay.text}`,
                            x: weekday.x,
                            y: weekday.y,
                            width: Math.max(weekday.width, candidates[0].monthDay.width),
                        };
                    })
                    .filter(Boolean);

                return [...combined, ...paired];
            }"""
        )

        headers: list[CalendarHeader] = []
        seen: set[tuple[str, int]] = set()
        for raw_header in raw_headers:
            key = (raw_header["text"], round(raw_header["x"]))
            if key in seen:
                continue
            seen.add(key)
            headers.append(
                CalendarHeader(
                    text=raw_header["text"],
                    column_center_x=raw_header["x"] + (raw_header["width"] / 2),
                    slot_date=datetime.strptime(
                        f"{raw_header['text']} {target_year}",
                        "%a %b %d %Y",
                    ).date(),
                )
            )

        return sorted(headers, key=lambda header: header.column_center_x)

    def _resolve_slot_date(self, headers: list[CalendarHeader], center_x: float) -> date | None:
        if not headers:
            return None
        return min(headers, key=lambda header: abs(header.column_center_x - center_x)).slot_date

    def _parse_slot_time(self, summary: str) -> time | None:
        match = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", summary, re.IGNORECASE)
        if not match:
            return None
        normalized_time = match.group(1).upper().replace(" ", "")
        return datetime.strptime(normalized_time, "%I:%M%p").time()

    async def _wait_for_active_modal(self, page: Page, text_hint: str | None = None) -> Locator:
        modals = page.locator("modal-container[role='dialog']")
        await modals.first.wait_for(state="visible", timeout=15_000)

        count = await modals.count()
        for index in range(count - 1, -1, -1):
            modal = modals.nth(index)
            if not await modal.is_visible():
                continue

            if text_hint is None:
                return modal

            hint_matches = modal.get_by_text(text_hint, exact=False)
            if await hint_matches.count():
                return modal

        return modals.last

    async def _find_clickable_action(self, card: Locator, label: str) -> Locator | None:
        normalized_label = re.sub(r"\s+", " ", label).strip()
        exact_pattern = re.compile(rf"^\s*{re.escape(normalized_label)}\s*$", re.IGNORECASE)
        contains_pattern = re.compile(rf"{re.escape(normalized_label)}", re.IGNORECASE)
        candidates = [
            card.get_by_role("button", name=exact_pattern),
            card.get_by_role("link", name=exact_pattern),
            card.get_by_role("button", name=contains_pattern),
            card.get_by_role("link", name=contains_pattern),
            card.get_by_text(label, exact=True),
            card.get_by_text(contains_pattern),
            card.locator("button").filter(has_text=contains_pattern),
            card.locator("a").filter(has_text=contains_pattern),
            card.locator("[role='button']").filter(has_text=contains_pattern),
        ]

        for candidate in candidates:
            count = await candidate.count()
            for index in range(count):
                locator = candidate.nth(index)
                if await locator.is_visible():
                    return locator
        return None

    async def _wait_for_clickable_action(
        self,
        page: Page,
        card: Locator,
        label: str,
        timeout_ms: int = 10_000,
    ) -> Locator | None:
        deadline = time_module.monotonic() + (timeout_ms / 1000)
        while time_module.monotonic() < deadline:
            locator = await self._find_clickable_action(card=card, label=label)
            if locator is not None:
                return locator
            await page.wait_for_timeout(250)
        return None

    async def _wait_for_booking_details_state(
        self,
        page: Page,
        card: Locator,
        timeout_ms: int = 15_000,
    ) -> tuple[str, Locator | None]:
        deadline = time_module.monotonic() + (timeout_ms / 1000)
        while time_module.monotonic() < deadline:
            book_button = await self._find_clickable_action(card=card, label="Book a Spot")
            if book_button is not None:
                return "bookable", book_button

            cancel_button = await self._find_clickable_action(card=card, label="Cancel Booking")
            if cancel_button is not None:
                return "already_booked", cancel_button

            await page.wait_for_timeout(100)

        return "unavailable", None

    async def _wait_for_post_booking_state(
        self,
        page: Page,
        timeout_ms: int = 10_000,
    ) -> tuple[str, object | None]:
        deadline = time_module.monotonic() + (timeout_ms / 1000)
        while True:
            not_yet_open_hint = await self._detect_not_yet_open(page=page)
            if not_yet_open_hint is not None:
                return "not_yet_open", not_yet_open_hint

            non_cancelable_confirmation = await self._detect_non_cancelable_confirmation(page=page)
            if non_cancelable_confirmation is not None:
                return "needs_confirmation", non_cancelable_confirmation

            success_confirmation = await self._detect_booking_success_confirmation(page=page)
            if success_confirmation is not None:
                return "booked", success_confirmation

            modals = page.locator("modal-container[role='dialog']")
            count = await modals.count()
            for index in range(count - 1, -1, -1):
                modal = modals.nth(index)
                if not await modal.is_visible():
                    continue
                prompt = modal.get_by_text("Please select 1 Spot", exact=False)
                if await prompt.count():
                    return "seat_selection", modal

            if time_module.monotonic() >= deadline:
                return "unknown", None

            await page.wait_for_timeout(100)

    async def _wait_for_seat_map_ready(self, page: Page, seat_modal: Locator, timeout_ms: int = 10_000) -> None:
        deadline = time_module.monotonic() + (timeout_ms / 1000)
        while time_module.monotonic() < deadline:
            if not await seat_modal.is_visible():
                await page.wait_for_timeout(250)
                continue

            done_button = await self._find_clickable_action(card=seat_modal, label="Done")
            prompt = seat_modal.get_by_text("Please select 1 Spot", exact=False)
            visible_preferred_seats = 0
            for seat_number in get_settings().preferred_seats:
                seat_text = seat_modal.get_by_text(str(seat_number), exact=True)
                if await seat_text.count():
                    visible_preferred_seats += 1

            loading_indicators = [
                seat_modal.get_by_text("Loading", exact=False),
                seat_modal.locator("[aria-label*='loading' i]"),
                seat_modal.locator("[role='status']"),
            ]
            loading_visible = False
            for indicator in loading_indicators:
                try:
                    if await indicator.count() and await indicator.first.is_visible():
                        loading_visible = True
                        break
                except Exception:
                    continue

            if await prompt.count() and done_button is not None and visible_preferred_seats >= 3 and not loading_visible:
                await page.wait_for_timeout(500)
                return True

            await page.wait_for_timeout(250)

        return False

    async def _wait_for_success_signal(self, page: Page, timeout_ms: int = 5_000) -> bool:
        deadline = time_module.monotonic() + (timeout_ms / 1000)
        while True:
            if await self._page_contains_success_signal(page):
                return True
            if time_module.monotonic() >= deadline:
                return False
            await page.wait_for_timeout(100)

    async def _detect_not_yet_open(self, page: Page, timeout_ms: int = 0) -> BookingGateHint | None:
        patterns = [
            r"(?:can't|cannot)\s+be\s+booked\s+until\s+(?P<when>[^.]+)",
            r"can\s+only\s+be\s+booked\s+starting\s+(?P<when>[^.]+)",
            r"available\s+to\s+book\s+(?:on|until)\s+(?P<when>[^.]+)",
            r"booking\s+opens\s+(?P<when>[^.]+)",
        ]
        deadline = time_module.monotonic() + (timeout_ms / 1000)

        while True:
            text_candidates: list[str] = []
            toast_like_locators = [
                page.locator("[role='alert']"),
                page.locator("[aria-live='polite']"),
                page.locator("[aria-live='assertive']"),
                page.locator(".toast"),
                page.locator(".toast-message"),
                page.locator(".ngx-toastr"),
                page.locator(".snackbar"),
                page.locator(".mat-snack-bar-container"),
            ]
            for locator in toast_like_locators:
                try:
                    if await locator.count():
                        text_candidates.extend(
                            [
                                re.sub(r"\s+", " ", text).strip()
                                for text in await locator.all_inner_texts()
                                if text.strip()
                            ]
                        )
                except Exception:
                    continue

            try:
                body_text = await page.locator("body").inner_text()
                if body_text.strip():
                    text_candidates.append(re.sub(r"\s+", " ", body_text).strip())
            except Exception:
                pass

            for candidate_text in text_candidates:
                for pattern in patterns:
                    match = re.search(pattern, candidate_text, re.IGNORECASE)
                    if not match:
                        continue

                    when_text = match.group("when").strip(" .")
                    parsed_hint = self._parse_human_when(when_text)
                    if parsed_hint is not None:
                        parsed_when, has_exact_time = parsed_hint
                        formatted_when = (
                            parsed_when.isoformat(sep=" ", timespec="minutes")
                            if has_exact_time
                            else parsed_when.date().isoformat()
                        )
                        return BookingGateHint(
                            message=(
                                f"This class is not bookable yet. Parsed opening time hint: "
                                f"{formatted_when}. Site message: '{when_text}'."
                            ),
                            available_at=parsed_when,
                            is_exact_time=has_exact_time,
                        )
                    return BookingGateHint(
                        message=f"This class is not bookable yet. Site message: '{when_text}'."
                    )

            if time_module.monotonic() >= deadline:
                return None

            await page.wait_for_timeout(200)

    async def _detect_non_cancelable_confirmation(self, page: Page, timeout_ms: int = 0) -> str | None:
        patterns = [
            r"you\s+will\s+not\s+be\s+able\s+to\s+cancel\s+this\s+booking",
            r"cannot\s+cancel\s+this\s+booking",
            r"non[-\s]?cancelable",
            r"non[-\s]?refundable",
        ]
        deadline = time_module.monotonic() + (timeout_ms / 1000)

        while True:
            text_candidates: list[str] = []
            modal_like_locators = [
                page.locator("modal-container[role='dialog']"),
                page.locator("[role='dialog']"),
                page.locator("[role='alertdialog']"),
                page.locator("[role='alert']"),
            ]
            for locator in modal_like_locators:
                try:
                    if await locator.count():
                        text_candidates.extend(
                            [
                                re.sub(r"\s+", " ", text).strip()
                                for text in await locator.all_inner_texts()
                                if text.strip()
                            ]
                        )
                except Exception:
                    continue

            try:
                body_text = await page.locator("body").inner_text()
                if body_text.strip():
                    text_candidates.append(re.sub(r"\s+", " ", body_text).strip())
            except Exception:
                pass

            for candidate_text in text_candidates:
                for pattern in patterns:
                    match = re.search(pattern, candidate_text, re.IGNORECASE)
                    if match:
                        return match.group(0)

            if time_module.monotonic() >= deadline:
                return None

            await page.wait_for_timeout(200)

    async def _detect_booking_success_confirmation(self, page: Page, timeout_ms: int = 0) -> str | None:
        patterns = [
            r"you\s+are\s+booked(?:\s+for[^.]*)?",
            r"reservation\s+confirmed",
            r"booking\s+successful",
        ]
        deadline = time_module.monotonic() + (timeout_ms / 1000)

        while True:
            text_candidates: list[str] = []
            modal_like_locators = [
                page.locator("modal-container[role='dialog']"),
                page.locator("[role='dialog']"),
                page.locator("[role='alertdialog']"),
            ]
            for locator in modal_like_locators:
                try:
                    if await locator.count():
                        text_candidates.extend(
                            [
                                re.sub(r"\s+", " ", text).strip()
                                for text in await locator.all_inner_texts()
                                if text.strip()
                            ]
                        )
                except Exception:
                    continue

            try:
                body_text = await page.locator("body").inner_text()
                if body_text.strip():
                    text_candidates.append(re.sub(r"\s+", " ", body_text).strip())
            except Exception:
                pass

            for candidate_text in text_candidates:
                for pattern in patterns:
                    match = re.search(pattern, candidate_text, re.IGNORECASE)
                    if match:
                        return match.group(0)

            if time_module.monotonic() >= deadline:
                return None

            await page.wait_for_timeout(200)

    def _parse_human_when(self, raw_when: str) -> tuple[datetime, bool] | None:
        settings = get_settings()
        now = datetime.now(settings.timezone).replace(tzinfo=None)
        normalized = re.sub(r"\s+", " ", raw_when).strip()
        normalized = re.sub(r"\bat\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        if re.search(r"\btomorrow\b", normalized, re.IGNORECASE):
            return now + timedelta(days=1), False

        formats = [
            ("%A %b %d %I:%M %p", True),
            ("%A, %b %d %I:%M %p", True),
            ("%a %b %d %I:%M %p", True),
            ("%a, %b %d %I:%M %p", True),
            ("%A %b %d", False),
            ("%A, %b %d", False),
            ("%a %b %d", False),
            ("%a, %b %d", False),
        ]
        for fmt, has_exact_time in formats:
            try:
                parsed = datetime.strptime(normalized, fmt)
            except ValueError:
                continue

            parsed = parsed.replace(year=now.year)
            if parsed < now - timedelta(days=1):
                parsed = parsed.replace(year=now.year + 1)
            return parsed, has_exact_time

        return None

    async def _select_seat_from_candidates(
        self,
        page: Page,
        seat_modal: Locator,
        seat_labels: list[str],
    ) -> str | None:
        for seat_label in seat_labels:
            seat_locators = await self._find_seat_locators(seat_modal=seat_modal, seat_label=seat_label)
            for seat_locator in seat_locators:
                if not await self._seat_looks_selectable(seat_locator):
                    continue

                if await self._try_select_seat(page=page, seat_modal=seat_modal, seat_locator=seat_locator):
                    return seat_label

        return None

    async def _select_any_available_seat(self, page: Page, seat_modal: Locator) -> str | None:
        seat_candidates = seat_modal.locator(".booking-layout-image, .cursor-pointer")
        count = await seat_candidates.count()
        seen_keys: set[str] = set()

        for index in range(count):
            seat_locator = seat_candidates.nth(index)
            if not await seat_locator.is_visible():
                continue
            locator_text = await seat_locator.evaluate(
                "(node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim()"
            )
            if re.search(r"\b(book|cancel|close|done|next|back)\b", locator_text, re.IGNORECASE):
                continue
            if not await self._seat_looks_selectable(seat_locator):
                continue

            html = await seat_locator.evaluate("(node) => node.outerHTML || node.textContent || ''")
            key = re.sub(r"\s+", " ", html).strip()
            if key in seen_keys:
                continue
            seen_keys.add(key)

            seat_label = await self._extract_seat_label_from_locator(seat_locator)
            if await self._try_select_seat(page=page, seat_modal=seat_modal, seat_locator=seat_locator):
                return seat_label or "available seat"

        return None

    async def _extract_seat_label_from_locator(self, seat_locator: Locator) -> str:
        return await seat_locator.evaluate(
            """(node) => {
                const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const plausibleLabel = (value) => /^[A-Za-z]?\\d+[A-Za-z]?$/.test(value) && value.length <= 8;
                const roots = [];
                let current = node;
                for (let depth = 0; depth < 5 && current; depth += 1) {
                    roots.push(current);
                    current = current.parentElement;
                }

                for (const root of roots) {
                    const texts = Array.from(root.querySelectorAll("*"))
                        .map((el) => normalize(el.textContent))
                        .filter(Boolean);
                    const exactMatch = texts.find(plausibleLabel);
                    if (exactMatch) {
                        return exactMatch;
                    }
                }

                const ownText = normalize(node.textContent);
                return plausibleLabel(ownText) ? ownText : "";
            }"""
        )

    async def _find_seat_locators(self, seat_modal: Locator, seat_label: str) -> list[Locator]:
        seat_text = str(seat_label)
        text_matches = seat_modal.get_by_text(seat_text, exact=True)
        results: list[Locator] = []

        count = await text_matches.count()
        for index in range(count):
            text_locator = text_matches.nth(index)
            if not await text_locator.is_visible():
                continue

            candidates = [
                text_locator.locator(
                    "xpath=ancestor::div[contains(@class, 'flex') and contains(@class, 'flex-col')][1]//div[contains(@class, 'booking-layout-image')][1]"
                ),
                text_locator.locator(
                    "xpath=../following-sibling::div[contains(@class, 'cursor-pointer')][1]"
                ),
                text_locator.locator(
                    "xpath=ancestor::div[contains(@class, 'flex') and contains(@class, 'flex-col')][1]//div[contains(@class, 'cursor-pointer')][1]"
                ),
                text_locator.locator(
                    "xpath=ancestor::div[contains(@class, 'mt-2')][1]//div[contains(@class, 'cursor-pointer')][1]"
                ),
            ]

            for candidate in candidates:
                candidate_count = await candidate.count()
                for candidate_index in range(candidate_count):
                    locator = candidate.nth(candidate_index)
                    if await locator.is_visible():
                        results.append(locator)

        deduped_results: list[Locator] = []
        seen_keys: set[str] = set()
        for locator in results:
            html = await locator.evaluate("(node) => node.outerHTML || node.textContent || ''")
            key = re.sub(r"\s+", " ", html).strip()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped_results.append(locator)

        return deduped_results

    async def _try_select_seat(self, page: Page, seat_modal: Locator, seat_locator: Locator) -> bool:
        click_targets = [
            seat_locator.locator(".booking-layout-image, button, [role='button'], img, svg").first,
            seat_locator,
        ]

        for click_target in click_targets:
            if await click_target.count() == 0:
                continue

            target = click_target
            try:
                if not await target.is_visible():
                    continue
            except Exception:
                continue

            try:
                await target.click(timeout=3_000)
            except Exception:
                try:
                    await target.click(timeout=3_000, force=True)
                except Exception:
                    continue

            await page.wait_for_timeout(500)
            done_button = await self._find_clickable_action(card=seat_modal, label="Done")
            if done_button is not None and await done_button.is_enabled():
                return True

        return False

    async def _seat_looks_selectable(self, seat_locator: Locator) -> bool:
        disabled_attr = await seat_locator.get_attribute("disabled")
        aria_disabled = await seat_locator.get_attribute("aria-disabled")
        class_name = (await seat_locator.get_attribute("class")) or ""
        lowered_class_name = class_name.lower()

        if disabled_attr is not None:
            return False
        if aria_disabled and aria_disabled.lower() == "true":
            return False
        if "booked-booking-layout" in lowered_class_name:
            return False
        if "disabled" in lowered_class_name:
            return False
        return "cursor-pointer" in lowered_class_name or "booking-layout-image" in lowered_class_name

    async def _page_contains_success_signal(self, page: Page) -> bool:
        success_signals = [
            "Booked",
            "Reserved",
            "Reservation Confirmed",
            "You are booked",
            "Cancel Booking",
            "Booking Successful",
        ]
        for signal in success_signals:
            locator = page.get_by_text(signal, exact=False)
            if await locator.count():
                return True
        return False

    async def _build_result_message(
        self,
        page: Page,
        class_name: str,
        seat_label: str | None,
    ) -> str:
        if await self._page_contains_success_signal(page):
            if seat_label is not None:
                return f"Seat {seat_label} is confirmed for {class_name}."
            return f"{class_name} was booked successfully."

        if seat_label is None:
            return (
                f"BookGym tried to book {class_name}, but could not confirm the booking. "
                "Please review the saved screenshot."
            )
        return (
            f"BookGym selected seat {seat_label} for {class_name}, but could not confirm the booking. "
            "Please review the saved screenshot."
        )
