from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    auth_state_path = Path(settings.auth_state_path)
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(settings.login_url)

        print("Log into the gym site in the opened browser window.")
        print("After you land on the account or class page, press Enter here to save the session.")
        input()

        await context.storage_state(path=str(auth_state_path))
        await browser.close()

    print(f"Saved auth state to {auth_state_path}")

if __name__ == "__main__":
    asyncio.run(main())
