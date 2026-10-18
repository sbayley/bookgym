from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.adapters.gym_site import ExampleGymAdapter
from app.config import get_settings
from app.gym_auth import auth_state_context_kwargs, ensure_auth_state_directory, gym_auth_status


async def refresh_gym_session_async(headless: bool = True) -> dict[str, object]:
    settings = get_settings()
    auth_state = ensure_auth_state_directory()
    adapter = ExampleGymAdapter()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(**auth_state_context_kwargs())
        try:
            await adapter.ensure_logged_in(context)
            await context.storage_state(path=str(auth_state))
        finally:
            await context.close()
            await browser.close()

    return gym_auth_status()


def refresh_gym_session(headless: bool = True) -> dict[str, object]:
    return asyncio.run(refresh_gym_session_async(headless=headless))
