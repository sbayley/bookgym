from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, time
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.gym_site import ExampleGymAdapter
from app.config import get_settings
from app.db import init_db


SENSITIVE_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "code",
    "email",
    "id_token",
    "jwt",
    "password",
    "refresh_token",
    "session",
    "token",
    "username",
}
INTERESTING_PATTERN = re.compile(
    r"book|booking|reserve|reservation|capacity|spot|seat|layout|available|booked|unavailable|waitlist|class|schedule",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=date.fromisoformat("2026-07-09"))
    parser.add_argument("--class-name", default="Pilates")
    parser.add_argument("--time", type=time.fromisoformat, default=time.fromisoformat("08:00"))
    parser.add_argument("--calendar-event-id", type=int)
    parser.add_argument(
        "--select-seat",
        action="store_true",
        help="Click a seat in the seat modal, but never click Done.",
    )
    parser.add_argument(
        "--seat-number",
        type=int,
        help="Seat number to try first when --select-seat is used.",
    )
    parser.add_argument(
        "--capture-page-load",
        action="store_true",
        help="Capture network responses during calendar load too, not just from the seat flow onward.",
    )
    return parser.parse_args()


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_KEYS:
            query.append((key, "<redacted>"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def sanitize_text(value: str) -> str:
    sanitized = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1<redacted>",
        value,
    )
    sanitized = re.sub(
        r"(?i)(access_token|refresh_token|id_token|password|authorization|session|jwt)"
        r'(["\']?\s*[:=]\s*["\']?)[^,"\'&\s}]+',
        r"\1\2<redacted>",
        sanitized,
    )
    sanitized = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<redacted-email>", sanitized)
    return sanitized


async def capture_modal_snapshot(seat_modal, seat_numbers: list[int]) -> dict:
    return await seat_modal.evaluate(
        """(seatModal, seatNumbers) => {
            const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
            const clip = (value, max = 400) => {
                const normalized = normalize(value);
                return normalized.length > max ? normalized.slice(0, max) + "..." : normalized;
            };
            const summarize = (el) => ({
                tag: el.tagName.toLowerCase(),
                text: clip(el.textContent),
                className: typeof el.className === "string" ? clip(el.className, 200) : "",
                role: el.getAttribute("role") || "",
                ariaLabel: el.getAttribute("aria-label") || "",
                disabled: el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true",
            });
            const visibleElements = Array.from(seatModal.querySelectorAll("*")).filter((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return (
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style.visibility !== "hidden" &&
                    style.display !== "none"
                );
            });
            const seatMatches = {};
            for (const seatNumber of seatNumbers) {
                const label = String(seatNumber);
                seatMatches[label] = visibleElements
                    .filter((el) => normalize(el.textContent) === label)
                    .slice(0, 6)
                    .map((el) => ({
                        self: summarize(el),
                        parent: el.parentElement ? summarize(el.parentElement) : null,
                        clickableAncestor: (() => {
                            let current = el;
                            while (current) {
                                const className = typeof current.className === "string" ? current.className : "";
                                if (
                                    current.tagName === "BUTTON" ||
                                    current.tagName === "A" ||
                                    current.getAttribute("role") === "button" ||
                                    /cursor-pointer|booking-layout-image|booked-booking-layout/i.test(className)
                                ) {
                                    return summarize(current);
                                }
                                current = current.parentElement;
                            }
                            return null;
                        })(),
                    }));
            }

            return {
                capturedAt: new Date().toISOString(),
                modalText: clip(seatModal.textContent, 3000),
                promptPresent: visibleElements.some((el) => normalize(el.textContent).includes("Please select 1 Spot")),
                doneButton: (() => {
                    const done = visibleElements.find((el) => normalize(el.textContent) === "Done");
                    return done ? summarize(done) : null;
                })(),
                selectableSeatGlyphCount: visibleElements.filter((el) => {
                    const className = typeof el.className === "string" ? el.className : "";
                    return /cursor-pointer|booking-layout-image/i.test(className);
                }).length,
                bookedSeatGlyphCount: visibleElements.filter((el) => {
                    const className = typeof el.className === "string" ? el.className : "";
                    return /booked-booking-layout/i.test(className);
                }).length,
                seatMatches,
            };
        }""",
        seat_numbers,
    )


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    init_db()

    auth_state_path = Path(settings.auth_state_path)
    if not auth_state_path.exists():
        print(f"Saved auth state not found at {auth_state_path}. Run python scripts/login_once.py first.")
        return

    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(settings.timezone).strftime("%Y%m%d-%H%M%S")
    artifact_stem = f"seat-network-{args.date.isoformat()}-{stamp}"
    network_path = artifacts_dir / f"{artifact_stem}.json"
    screenshot_path = artifacts_dir / f"{artifact_stem}.png"
    html_path = artifacts_dir / f"{artifact_stem}.html"

    events: list[dict] = []
    response_tasks: list[asyncio.Task] = []
    seat_modal_snapshot: dict | None = None
    selected_seat_number: int | None = None
    stage = "starting"
    gate_message = ""
    matched_card_summary = ""

    async def capture_response(response) -> None:
        request = response.request
        resource_type = request.resource_type
        if resource_type not in {"document", "fetch", "xhr"}:
            return

        event = {
            "method": request.method,
            "resource_type": resource_type,
            "status": response.status,
            "url": sanitize_url(response.url),
            "request_post_data": sanitize_text(request.post_data or ""),
            "response_content_type": response.headers.get("content-type", ""),
            "interesting": bool(INTERESTING_PATTERN.search(response.url)),
            "body_preview": "",
            "body_error": "",
        }

        content_type = event["response_content_type"].lower()
        if "json" in content_type or "text" in content_type or "html" in content_type:
            try:
                body = sanitize_text(await response.text())
                if INTERESTING_PATTERN.search(body):
                    event["interesting"] = True
                event["body_preview"] = body[:20_000]
            except Exception as exc:
                event["body_error"] = str(exc)

        events.append(event)

    adapter = ExampleGymAdapter()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context(storage_state=str(auth_state_path))
        try:
            await adapter.ensure_logged_in(context)

            if args.capture_page_load:
                context.on(
                    "response",
                    lambda response: response_tasks.append(asyncio.create_task(capture_response(response))),
                )

            page, _headers = await adapter.open_calendar_week_containing(
                context=context,
                target_date=args.date,
            )
            stage = "calendar_opened"

            candidate = await adapter._find_booking_candidate(
                page=page,
                class_name=args.class_name,
                class_date=args.date,
                window_start_time=args.time,
                window_end_time=args.time,
                calendar_event_id=args.calendar_event_id,
            )
            if candidate is None:
                stage = "class_not_found"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(
                    f"Could not find {args.class_name!r} on {args.date.isoformat()} "
                    f"at {args.time.isoformat(timespec='minutes')}."
                )
                return

            matched_card_summary = candidate.summary
            print("Matched class:", candidate.summary.replace("\n", " | "))
            print("Matched calendar event id:", candidate.calendar_event_id or "unknown")

            book_button = await adapter._find_clickable_action(card=candidate.locator, label="Book Now")
            if book_button is None:
                stage = "book_now_missing"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print("Matched class, but could not find Book Now.")
                return

            await book_button.click()
            details_modal = await adapter._wait_for_active_modal(page=page, text_hint=args.class_name)
            stage = "details_modal_opened"

            book_a_spot_button = await adapter._wait_for_clickable_action(
                page=page,
                card=details_modal,
                label="Book a Spot",
                timeout_ms=15_000,
            )
            if book_a_spot_button is None:
                stage = "book_a_spot_missing"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print("Could not find Book a Spot in the details modal.")
                return

            if not args.capture_page_load:
                context.on(
                    "response",
                    lambda response: response_tasks.append(asyncio.create_task(capture_response(response))),
                )

            await book_a_spot_button.click()
            stage = "book_a_spot_clicked"

            not_yet_open_hint = await adapter._detect_not_yet_open(page=page, timeout_ms=4_000)
            if not_yet_open_hint is not None:
                stage = "booking_gate_not_yet_open"
                gate_message = not_yet_open_hint.message
                print("Detected booking gate:", gate_message)
            else:
                confirmation = await adapter._detect_non_cancelable_confirmation(page=page, timeout_ms=1_500)
                if confirmation is not None:
                    stage = "booking_gate_confirmation"
                    gate_message = confirmation
                    print("Detected confirmation gate:", confirmation)
                else:
                    seat_modal = await adapter._wait_for_active_modal(
                        page=page,
                        text_hint="Please select 1 Spot",
                    )
                    stage = "seat_modal_opened"
                    await adapter._wait_for_seat_map_ready(page=page, seat_modal=seat_modal)
                    await page.wait_for_timeout(1_000)

                    seat_numbers = []
                    if args.seat_number is not None:
                        seat_numbers.append(args.seat_number)
                    for preferred in settings.preferred_seats:
                        if preferred not in seat_numbers:
                            seat_numbers.append(preferred)
                    seat_modal_snapshot = await capture_modal_snapshot(seat_modal, seat_numbers[:12])
                    html_path.write_text(
                        await seat_modal.evaluate("(el) => el.outerHTML"),
                        encoding="utf-8",
                    )

                    if args.select_seat:
                        if args.seat_number is not None:
                            seat_locators = await adapter._find_seat_locators(
                                seat_modal=seat_modal,
                                seat_number=args.seat_number,
                            )
                            for seat_locator in seat_locators:
                                if not await adapter._seat_looks_selectable(seat_locator):
                                    continue
                                if await adapter._try_select_seat(
                                    page=page,
                                    seat_modal=seat_modal,
                                    seat_locator=seat_locator,
                                ):
                                    selected_seat_number = args.seat_number
                                    break
                        if selected_seat_number is None:
                            selected_seat_number = await adapter._select_preferred_seat(
                                page=page,
                                seat_modal=seat_modal,
                            )
                        stage = "seat_clicked" if selected_seat_number is not None else "seat_click_failed"
                        await page.wait_for_timeout(1_500)
                        seat_modal_snapshot = await capture_modal_snapshot(seat_modal, seat_numbers[:12])

            await page.wait_for_timeout(2_000)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)
            await page.screenshot(path=str(screenshot_path), full_page=True)
            await context.storage_state(path=str(auth_state_path))
        finally:
            await context.close()
            await browser.close()

    payload = {
        "target": {
            "date": args.date.isoformat(),
            "class_name": args.class_name,
            "time": args.time.isoformat(timespec="minutes"),
            "select_seat": args.select_seat,
            "seat_number": args.seat_number,
        },
        "stage": stage,
        "gate_message": gate_message,
        "selected_seat_number": selected_seat_number,
        "matched_card_summary": matched_card_summary,
        "captured_response_count": len(events),
        "captured_interesting_response_count": sum(1 for event in events if event["interesting"]),
        "events": events,
        "seat_modal_snapshot": seat_modal_snapshot,
        "screenshot_path": str(screenshot_path),
        "html_path": str(html_path) if html_path.exists() else "",
    }
    network_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Saved seat-network artifact: {network_path}")
    print(f"Saved screenshot to {screenshot_path}")
    if html_path.exists():
        print(f"Saved seat modal HTML to {html_path}")


if __name__ == "__main__":
    asyncio.run(main())
