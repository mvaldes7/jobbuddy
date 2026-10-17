"""Playwright browser lifecycle. Plain Chromium, honest defaults, no stealth/evasion plugins."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator


@asynccontextmanager
async def open_browser(headless: bool = True, timeout_ms: int = 45000) -> AsyncIterator[object]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US",
                                            timezone_id="America/Los_Angeles")
        context.set_default_timeout(timeout_ms)
        try:
            yield context
        finally:
            await context.close()
            await browser.close()
