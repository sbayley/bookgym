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
    r"book|booking|reserve|reservation|capacity|spot|seat|open|available|waitlist|class|schedule",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat, default=date.fromisoformat("2026-07-09"))
    parser.add_argument("--class-name", default="Pilates")
    parser.add_argument("--time", type=time.fromisoformat, default=time.fromisoformat("08:00"))
    parser.add_argument("--calendar-event-id", type=int)
    parser.add_argument(
        "--details-only",
        action="store_true",
        help="Stop after opening the class details modal; do not click Book a Spot.",
    )
    parser.add_argument(
        "--capture-page-load",
        action="store_true",
        help="Capture network responses while loading/navigating the calendar, not just after the class match.",
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
    network_path = artifacts_dir / f"booking-network-{args.date.isoformat()}-{stamp}.json"
    screenshot_path = artifacts_dir / f"booking-network-{args.date.isoformat()}-{stamp}.png"

    events: list[dict] = []
    response_tasks: list[asyncio.Task] = []
    matched_card_html_preview = ""

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
            "interesting": False,
            "body_preview": "",
            "body_error": "",
        }

        content_type = event["response_content_type"].lower()
        if "json" in content_type or "text" in content_type or "html" in content_type:
            try:
                body = await response.text()
                body = sanitize_text(body)
                event["interesting"] = bool(INTERESTING_PATTERN.search(body) or INTERESTING_PATTERN.search(response.url))
                if event["interesting"]:
                    event["body_preview"] = body[:20_000]
            except Exception as exc:
                event["body_error"] = str(exc)
        else:
            event["interesting"] = bool(INTERESTING_PATTERN.search(response.url))

        if event["interesting"]:
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
            candidate = await adapter._find_booking_candidate(
                page=page,
                class_name=args.class_name,
                class_date=args.date,
                window_start_time=args.time,
                window_end_time=args.time,
                calendar_event_id=args.calendar_event_id,
            )
            if candidate is None:
                print(
                    f"Could not find {args.class_name!r} on {args.date.isoformat()} "
                    f"at {args.time.isoformat(timespec='minutes')}."
                )
                await page.screenshot(path=str(screenshot_path), full_page=True)
                return

            try:
                matched_card_html_preview = sanitize_text(
                    await candidate.locator.evaluate("(node) => node.outerHTML || ''")
                )[:20_000]
            except Exception:
                matched_card_html_preview = ""

            if not args.capture_page_load:
                context.on(
                    "response",
                    lambda response: response_tasks.append(asyncio.create_task(capture_response(response))),
                )
            print("Matched class:", candidate.summary.replace("\n", " | "))
            print("Matched calendar event id:", candidate.calendar_event_id or "unknown")

            book_button = await adapter._find_clickable_action(card=candidate.locator, label="Book Now")
            if book_button is None:
                print("Matched class, but could not find Book Now.")
                await page.screenshot(path=str(screenshot_path), full_page=True)
                return

            await book_button.click()
            details_modal = await adapter._wait_for_active_modal(page=page, text_hint=args.class_name)
            print("Opened class details modal.")

            if not args.details_only:
                book_a_spot_button = await adapter._wait_for_clickable_action(
                    page=page,
                    card=details_modal,
                    label="Book a Spot",
                    timeout_ms=15_000,
                )
                if book_a_spot_button is None:
                    print("Could not find Book a Spot in the details modal.")
                else:
                    await book_a_spot_button.click()
                    print("Clicked Book a Spot; waiting for booking gate/status.")
                    not_yet_open_hint = await adapter._detect_not_yet_open(page=page, timeout_ms=5_000)
                    if not_yet_open_hint is not None:
                        print("Detected booking gate:", not_yet_open_hint.message)
                    else:
                        confirmation = await adapter._detect_non_cancelable_confirmation(page=page, timeout_ms=1_500)
                        if confirmation is not None:
                            print("Detected confirmation gate:", confirmation)
                        else:
                            print("No booking gate text detected; stopped before selecting any seat.")

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
            "calendar_event_id": args.calendar_event_id,
        },
        "captured_interesting_response_count": len(events),
        "matched_card_html_preview": matched_card_html_preview,
        "events": events,
        "screenshot_path": str(screenshot_path),
    }
    network_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Saved network artifact: {network_path}")
    print(f"Saved screenshot: {screenshot_path}")
    print(f"Captured interesting response(s): {len(events)}")
    for index, event in enumerate(events[:12], start=1):
        print(f"{index}. {event['method']} {event['status']} {event['resource_type']} {event['url']}")


if __name__ == "__main__":
    asyncio.run(main())
