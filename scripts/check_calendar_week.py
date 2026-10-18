from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
import sys

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.gym_site import ExampleGymAdapter
from app.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_date", type=date.fromisoformat, help="YYYY-MM-DD")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    auth_state_path = Path(settings.auth_state_path)
    if not auth_state_path.exists():
        print(f"Saved auth state not found at {auth_state_path}. Run python scripts/login_once.py first.")
        return

    adapter = ExampleGymAdapter()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context(storage_state=str(auth_state_path))
        try:
            await adapter.ensure_logged_in(context)
            page, headers = await adapter.open_calendar_week_containing(
                context=context,
                target_date=args.target_date,
            )
            visible_dates = sorted(header.slot_date for header in headers)
            if visible_dates:
                print("Loaded week dates: " + ", ".join(day.isoformat() for day in visible_dates))
                print(f"Target visible: {args.target_date in visible_dates}")
            else:
                print("Loaded week dates: unavailable")
            await page.close()
        finally:
            await context.storage_state(path=str(auth_state_path))
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
