from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    auth_state_path = Path(settings.auth_state_path)

    if not auth_state_path.exists():
        raise FileNotFoundError(
            f"Saved auth state not found at {auth_state_path}. Run python scripts/login_once.py first."
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=str(auth_state_path))
        page = await context.new_page()
        await page.goto(settings.base_url)

        print(f"Loaded: {page.url}")
        try:
            await page.get_by_role("link", name="Book").click(timeout=10_000)
        except PlaywrightTimeoutError:
            print("Could not find a link with the accessible name 'Book'.")
            print("If the page is open, click into the booking area manually and press Enter here.")
            input()
        else:
            await page.wait_for_load_state("networkidle")

        print(f"Current URL: {page.url}")
        print("If this is the correct classes page, press Enter to save a screenshot and close.")
        input()

        artifacts_dir = PROJECT_ROOT / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = artifacts_dir / "book-page.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Saved screenshot to {screenshot_path}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
