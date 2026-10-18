from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.gym_site import ExampleGymAdapter
from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    adapter = ExampleGymAdapter()
    auth_state_path = Path(settings.auth_state_path)
    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if not auth_state_path.exists():
        raise FileNotFoundError(
            f"Saved auth state not found at {auth_state_path}. Run python scripts/login_once.py first."
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=str(auth_state_path))
        page = await context.new_page()

        await adapter.ensure_logged_in(context)
        await page.goto(settings.calendar_url)
        await page.wait_for_load_state("networkidle")

        card = await adapter._find_first_available_class_card(page=page, class_name="Yoga Level 1")
        if card is None:
            raise RuntimeError("Could not find a visible Yoga Level 1 class card.")

        book_now = await adapter._find_clickable_action(card=card, label="Book Now")
        if book_now is None:
            raise RuntimeError("Could not find the schedule-page 'Book Now' action.")

        await book_now.click()
        details_modal = await adapter._wait_for_active_modal(page=page, text_hint="Yoga Level 1")
        book_a_spot = await adapter._wait_for_clickable_action(
            page=page,
            card=details_modal,
            label="Book a Spot",
            timeout_ms=15_000,
        )
        if book_a_spot is None:
            raise RuntimeError("Could not find the details-modal 'Book a Spot' action.")

        await book_a_spot.click()
        seat_modal = await adapter._wait_for_active_modal(page=page, text_hint="Please select 1 Spot")
        await adapter._wait_for_seat_map_ready(page=page, seat_modal=seat_modal)
        await page.wait_for_timeout(1_500)

        screenshot_path = artifacts_dir / "seat-debug.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)

        modal_html = await seat_modal.evaluate("(el) => el.outerHTML")
        html_path = artifacts_dir / "seat-debug.html"
        html_path.write_text(modal_html, encoding="utf-8")

        debug_payload = await seat_modal.evaluate(
            """(seatModal, seatNumbers) => {
                const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const clip = (value, max = 400) => {
                    const normalized = normalize(value);
                    return normalized.length > max ? normalized.slice(0, max) + "..." : normalized;
                };
                const rectFor = (el) => {
                    const rect = el.getBoundingClientRect();
                    return {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                    };
                };
                const summarize = (el) => ({
                    tag: el.tagName.toLowerCase(),
                    text: clip(el.textContent),
                    className: typeof el.className === "string" ? clip(el.className, 200) : "",
                    role: el.getAttribute("role") || "",
                    ariaLabel: el.getAttribute("aria-label") || "",
                    disabled: el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true",
                    rect: rectFor(el),
                    html: clip(el.outerHTML, 500),
                });
                const allElements = Array.from(seatModal.querySelectorAll("*"));
                const clickable = allElements
                    .filter((el) => el.tagName === "BUTTON" || el.tagName === "A" || el.getAttribute("role") === "button")
                    .map(summarize);
                const iconish = allElements
                    .filter((el) => {
                        const tag = el.tagName.toLowerCase();
                        const cls = typeof el.className === "string" ? el.className.toLowerCase() : "";
                        return tag === "img" || tag === "svg" || cls.includes("icon");
                    })
                    .map(summarize);
                const seats = {};
                for (const seatNumber of seatNumbers) {
                    const seatText = String(seatNumber);
                    const matches = allElements.filter((el) => normalize(el.textContent) === seatText);
                    seats[seatText] = matches.slice(0, 10).map((el) => ({
                        self: summarize(el),
                        parent: el.parentElement ? summarize(el.parentElement) : null,
                        previousSibling: el.previousElementSibling ? summarize(el.previousElementSibling) : null,
                        nextSibling: el.nextElementSibling ? summarize(el.nextElementSibling) : null,
                        clickableAncestor: (() => {
                            let current = el;
                            while (current) {
                                if (
                                    current.tagName === "BUTTON" ||
                                    current.tagName === "A" ||
                                    current.getAttribute("role") === "button"
                                ) {
                                    return summarize(current);
                                }
                                current = current.parentElement;
                            }
                            return null;
                        })(),
                        descendants: Array.from(el.querySelectorAll("button, a, [role='button'], img, svg"))
                            .slice(0, 8)
                            .map(summarize),
                    }));
                }
                return {
                    capturedAt: new Date().toISOString(),
                    modal: summarize(seatModal),
                    modalText: clip(seatModal.textContent, 3000),
                    clickable,
                    iconish,
                    seats,
                };
            }""",
            settings.preferred_seats,
        )

        debug_path = artifacts_dir / "seat-debug.json"
        debug_path.write_text(json.dumps(debug_payload, indent=2), encoding="utf-8")

        print(f"Saved screenshot to {screenshot_path}")
        print(f"Saved modal HTML to {html_path}")
        print(f"Saved seat debug JSON to {debug_path}")
        print("Leave the browser open if you want to inspect the seat modal manually, then press Enter here.")
        input()

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
