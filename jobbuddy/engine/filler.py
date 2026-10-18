"""Multi-step form filling loop.

Invariant: the final Submit button is NEVER clicked unless an ApprovalToken (issued only by
engine.submitter after verifying an approval record in the DB) is passed in.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jobbuddy.config import EngineConfig
from jobbuddy.engine.detectors import detect_blockers
from jobbuddy.engine.field_mapper import (FieldMapper, MapperContext, _norm, low_confidence_items, match_option,
                                         needs_cover_letter)
from jobbuddy.engine.form_inspector import frame_for, inspect_page
from jobbuddy.engine.human import Human
from jobbuddy.engine.models import Button, FieldMapping, FillResult, FormField, Option, StepRecord

log = logging.getLogger(__name__)

COMBO_WAIT_S = 5.0        # async autocompletes (e.g. Greenhouse location) can take a few seconds
COMBO_MAX_OPTIONS = 200


@dataclass(frozen=True)
class ApprovalToken:
    application_id: int
    approved_via: str
    _issued_by_submitter: bool = False

    def valid_for(self, application_id: int | None) -> bool:
        return self._issued_by_submitter and application_id is not None and application_id == self.application_id


@dataclass
class FillContext:
    mapper_ctx: MapperContext
    engine_cfg: EngineConfig
    screenshot_dir: Path
    application_id: int | None = None
    resume_path: Path | None = None
    cover_letter_path: Callable[[], Path | None] | None = None
    human: Human = field(default_factory=Human)
    on_cover_letter_needed: Callable[[], None] | None = None


class FormFiller:
    def __init__(self, ctx: FillContext) -> None:
        self.ctx = ctx
        self.mapper = FieldMapper(ctx.mapper_ctx)
        self.adapter = ctx.mapper_ctx.adapter

    async def _screenshot(self, page: Any, tag: str) -> str:
        self.ctx.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.ctx.screenshot_dir / f"app{self.ctx.application_id or 0}_{tag}_{int(time.time())}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

    async def _apply(self, page: Any, f: FormField, m: FieldMapping) -> str | None:
        if m.value in (None, "", []):
            return None
        frame = frame_for(page, f.frame_index)
        loc = frame.locator(f.selector).first
        h = self.ctx.human
        try:
            if f.type == "file":
                path = self._file_for(m)
                if path is None:
                    return f"{f.field_id}: no file available for {m.file_kind}"
                await loc.set_input_files(str(path))
            elif f.type == "select":
                try:
                    await loc.select_option(label=str(m.value))
                except Exception:
                    await loc.select_option(value=str(m.value))
            elif f.type == "custom_select" and f.tag == "input":
                return await self._fill_combobox(frame, loc, f, m)
            elif f.type == "custom_select":
                await loc.click()
                await h.pause(0.5)
                opt = frame.get_by_role("option", name=str(m.value), exact=True)
                if await opt.count() == 0:
                    opt = frame.locator('[role="option"]', has_text=str(m.value))
                await opt.first.click()
            elif f.type == "radio":
                target = next((o for o in f.options if o.label == m.value or o.value == m.value), None)
                if target and target.selector:
                    await frame.locator(target.selector).first.check()
            elif f.type == "checkbox_group":
                vals = m.value if isinstance(m.value, list) else [m.value]
                for o in f.options:
                    if o.label in vals and o.selector:
                        await frame.locator(o.selector).first.check()
            elif f.type == "checkbox":
                await loc.set_checked(bool(m.value))
            else:
                if str(f.current_value or "") == str(m.value):
                    return None
                await h.type_into(loc, str(m.value))
            await h.pause(0.4)
        except Exception as exc:
            if f.type == "custom_select" and f.tag == "input":  # nothing selected: keep it on the approval card
                m.value, m.confidence, m.reason = None, 0.0, f"dropdown fill failed ({type(exc).__name__})"
            return f"{f.field_id}: {type(exc).__name__}"
        return None

    # ---------- input[role=combobox] (Greenhouse react-select, autocompletes) ----------
    async def _listbox_scope(self, frame: Any, loc: Any) -> Any:
        """The listbox the combobox controls (react-select sets aria-controls while open), else the frame."""
        for attr in ("aria-controls", "aria-owns"):
            lb_id = await loc.get_attribute(attr)
            if lb_id:
                scope = frame.locator(f'[id="{lb_id}"]')
                if await scope.count():
                    return scope
        return frame

    async def _visible_options(self, frame: Any, loc: Any) -> list[str]:
        scope = await self._listbox_scope(frame, loc)
        opts = scope.locator('[role="option"]')
        out: list[str] = []
        for i in range(min(await opts.count(), COMBO_MAX_OPTIONS)):
            o = opts.nth(i)
            try:
                if await o.is_visible():
                    t = re.sub(r"\s+", " ", (await o.inner_text(timeout=1000)) or "").strip()
                    if t:
                        out.append(t)
            except Exception:
                continue
        return out

    async def _wait_options(self, frame: Any, loc: Any, timeout_s: float) -> list[str]:
        """Poll until options render (async autocompletes need a moment), then let the list settle."""
        deadline = time.monotonic() + timeout_s
        opts: list[str] = []
        while True:
            opts = await self._visible_options(frame, loc)
            if opts or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        if opts:
            await asyncio.sleep(0.4)
            opts = await self._visible_options(frame, loc) or opts
        return opts

    @staticmethod
    def _pick(value: str, labels: list[str]) -> str | None:
        opt, _score = match_option(value, [Option(label=t) for t in labels])
        return opt.label if opt else None

    async def _fill_combobox(self, frame: Any, loc: Any, f: FormField, m: FieldMapping) -> str | None:
        """Type to filter, then click the option whose accessible name is exactly the chosen label.
        Enter is only a fallback when that option is visible and first in the list. Typed text alone is
        never trusted: react-select discards it on blur unless an option was chosen."""
        value = str(m.value).strip()
        if not value:
            return None
        if f.current_value and _norm(str(f.current_value)) == _norm(value):
            return None
        h = self.ctx.human
        await h.type_into(loc, value)
        seen = await self._wait_options(frame, loc, COMBO_WAIT_S)
        target = self._pick(value, seen)
        if target is None:
            # Typed text matched nothing (e.g. "Authorized to work in the US" vs Yes/No): clear the filter
            # to see the full list, pick the best match there, then filter down to it.
            await loc.fill("")
            opts = await self._wait_options(frame, loc, 1.0)
            if not opts:
                await loc.press("ArrowDown")
                opts = await self._wait_options(frame, loc, 1.0)
            seen = seen or opts
            target = self._pick(value, opts)
            if target is not None:
                await loc.press_sequentially(target, delay=h.key_delay())
                await self._wait_options(frame, loc, COMBO_WAIT_S)
        if target is None:
            if not seen:
                # No options ever rendered: a free-text combobox. Keep the typed value.
                await h.type_into(loc, value)
                return None
            await loc.fill("")
            await loc.press("Escape")
            m.value, m.confidence = None, 0.0
            m.suggestions = seen[:8]
            m.reason = f"no option matches '{value[:60]}'"
            return f"{f.field_id}: no option matches"
        scope = await self._listbox_scope(frame, loc)
        opt = scope.get_by_role("option", name=target, exact=True)
        if await opt.count() and await opt.first.is_visible():
            await opt.first.click()
        else:
            visible = await self._visible_options(frame, loc)
            if visible and visible[0] == target:
                await loc.press("Enter")
            else:
                await loc.fill("")
                await loc.press("Escape")
                m.value, m.confidence = None, 0.0
                m.suggestions = (visible or seen)[:8]
                m.reason = f"option '{target[:60]}' not clickable"
                return f"{f.field_id}: option not clickable"
        if target != value:
            m.value = target  # the approval card shows what was actually selected
        await h.pause(0.4)
        return None

    def _file_for(self, m: FieldMapping) -> Path | None:
        if m.file_kind == "resume":
            return self.ctx.resume_path
        if m.file_kind == "cover_letter" and self.ctx.cover_letter_path:
            return self.ctx.cover_letter_path()
        return None

    async def run(self, page: Any, approval: ApprovalToken | None = None) -> FillResult:
        cfg = self.ctx.engine_cfg
        result = FillResult(status="awaiting_approval")
        prev_signature: tuple[str, ...] | None = None
        for step in range(cfg.max_steps):
            await self._settle(page)
            blockers = await detect_blockers(page)
            if blockers:
                result.status = "paused"
                result.blockers = blockers
                result.error = "human needed: " + ", ".join(sorted({b.kind for b in blockers}))
                result.screenshot_path = await self._screenshot(page, "blocked")
                result.final_url = page.url
                return result

            snap = await inspect_page(page, self.adapter.submit_automation_ids, self.adapter.next_automation_ids)
            signature = tuple(sorted(f.field_id for f in snap.fields)) + (snap.url,)
            if prev_signature is not None and signature == prev_signature and snap.error_texts:
                result.status = "paused"
                result.error = "validation errors after Next: " + "; ".join(snap.error_texts[:5])
                result.screenshot_path = await self._screenshot(page, "validation")
                result.final_url = page.url
                return result
            prev_signature = signature

            if needs_cover_letter(snap.fields) and self.ctx.on_cover_letter_needed:
                self.ctx.on_cover_letter_needed()
            mappings = await asyncio.to_thread(self.mapper.map_fields, snap.fields)
            errors = [e for e in [await self._apply(page, f, m) for f, m in zip(snap.fields, mappings)] if e]
            rec = StepRecord(index=step, url=snap.url, fields=snap.fields, mappings=mappings, errors=errors)
            result.steps.append(rec)
            result.low_confidence += low_confidence_items(snap.fields, mappings, cfg.min_confidence_autofill)

            live = [b for b in snap.buttons if not b.disabled]
            submit = next((b for b in live if b.kind == "submit"), None)
            nxt = next((b for b in live if b.kind == "next"), None)
            if submit is not None and nxt is None:
                rec.action = "stopped_before_submit"
                result.submit_button = submit
                result.final_url = page.url
                result.screenshot_path = await self._screenshot(page, "filled")
                if approval is None or not approval.valid_for(self.ctx.application_id):
                    result.status = "awaiting_approval"
                    return result
                return await self._submit(page, submit, result)
            if nxt is not None:
                rec.action = f"next:{nxt.text}"
                await self.ctx.human.pause()
                await frame_for(page, nxt.frame_index).locator(nxt.selector).first.click()
                continue
            rec.action = "no_navigation"
            result.status = "failed"
            result.error = "no Next or Submit button found"
            result.screenshot_path = await self._screenshot(page, "stuck")
            result.final_url = page.url
            return result
        result.status = "failed"
        result.error = f"exceeded max_steps={cfg.max_steps}"
        return result

    async def _submit(self, page: Any, submit: Button, result: FillResult) -> FillResult:
        """Only reachable with a valid ApprovalToken."""
        missing = [i for i in result.low_confidence if i["required"] and i["value"] in (None, "", [])]
        if missing:
            result.status = "paused"
            result.error = "required fields still empty: " + ", ".join(i["label"] for i in missing[:5])
            return result
        await self.ctx.human.pause()
        await frame_for(page, submit.frame_index).locator(submit.selector).first.click()
        await self._settle(page)
        await asyncio.sleep(2)
        blockers = await detect_blockers(page)
        if blockers:  # e.g. an invisible reCAPTCHA escalated to a challenge
            result.status = "paused"
            result.blockers = blockers
            result.error = "challenge after submit click: human needed"
            result.screenshot_path = await self._screenshot(page, "post_submit_blocked")
            return result
        text = ""
        for fr in page.frames:
            try:
                text += " " + (await fr.evaluate("() => document.body ? document.body.innerText : ''"))[:5000]
            except Exception:
                pass
        result.screenshot_path = await self._screenshot(page, "submitted")
        result.final_url = page.url
        if any(re.search(p, text, re.I) for p in self.adapter.confirmation_patterns):
            result.status = "submitted"
            result.confirmation_text = re.sub(r"\s+", " ", text).strip()[:300]
        else:
            result.status = "failed"
            result.error = "no confirmation detected after submit; verify manually"
        return result
