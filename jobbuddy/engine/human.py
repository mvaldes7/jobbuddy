"""Human-paced interaction. This is about being a polite, normal-speed client - NOT evasion:
no fingerprint spoofing, no stealth plugins, no CAPTCHA handling."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field


@dataclass
class Human:
    typing_delay_ms: tuple[int, int] = (45, 140)
    action_delay_ms: tuple[int, int] = (350, 1200)
    rng: random.Random = field(default_factory=random.Random)

    async def pause(self, scale: float = 1.0) -> None:
        lo, hi = self.action_delay_ms
        if hi <= 0:
            return
        await asyncio.sleep(self.rng.uniform(lo, hi) / 1000 * scale)

    def key_delay(self) -> float:
        lo, hi = self.typing_delay_ms
        return self.rng.uniform(lo, hi) if hi > 0 else 0

    async def type_into(self, locator, text: str) -> None:  # noqa: ANN001
        await locator.click()
        await locator.fill("")
        if self.typing_delay_ms[1] <= 0 or len(text) > 400:
            await locator.fill(text)  # long answers / test mode: paste-like
        else:
            await locator.press_sequentially(text, delay=self.key_delay())
        await self.pause(0.3)
