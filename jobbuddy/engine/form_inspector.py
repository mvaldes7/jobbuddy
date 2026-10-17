"""Extract every form field from a page, including open shadow roots and iframes (Greenhouse embeds).

Label resolution order (first non-empty wins):
  label[for=id] -> aria-labelledby -> wrapping <label> -> aria-label -> placeholder ->
  Workday formField container label -> nearest preceding text (siblings / ancestors) -> title -> name/id
Each control is stamped with data-jb-id so the filler can address it unambiguously in this page state.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from jobbuddy.engine.models import Button, FormField, Option, PageSnapshot

log = logging.getLogger(__name__)

EXTRACT_JS = r"""
(frameIndex) => {
  const prefix = 'f' + frameIndex + '-';
  window.__jbCounter = window.__jbCounter || 0;
  const roots = [];
  const collect = (root, inShadow) => {
    roots.push([root, inShadow]);
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collect(el.shadowRoot, true); });
  };
  collect(document, false);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = el => el ? clean(el.innerText !== undefined && el.innerText !== '' ? el.innerText : el.textContent) : '';
  const stamp = el => {
    if (!el.getAttribute('data-jb-id')) el.setAttribute('data-jb-id', prefix + (++window.__jbCounter));
    return el.getAttribute('data-jb-id');
  };
  const sel = el => '[data-jb-id="' + stamp(el) + '"]';
  const findById = (root, id) => {
    if (!id) return null;
    try { return (root.getElementById ? root.getElementById(id) : null) || root.querySelector('#' + CSS.escape(id)) || document.getElementById(id); }
    catch (e) { return null; }
  };
  const isVisible = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    let p = el.parentElement;
    while (p) { const ps = getComputedStyle(p); if (ps.display === 'none') return false; p = p.parentElement; }
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const ownLabelText = (el, root) => {
    if (el.id) {
      const l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]') ||
                document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && textOf(l)) return [textOf(l), 'for'];
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(i => textOf(findById(root, i))).filter(Boolean).join(' ');
      if (t) return [t, 'aria-labelledby'];
    }
    const wrap = el.closest('label');
    if (wrap) {
      const t = clean(Array.from(wrap.childNodes).filter(n => n !== el && !(n.tagName === 'SELECT')).map(n => n.textContent).join(' '));
      if (t) return [t, 'wrapping-label'];
    }
    const al = el.getAttribute('aria-label');
    if (al && clean(al)) return [clean(al), 'aria-label'];
    return null;
  };
  const nearestText = (el) => {
    const wdc = el.closest('[data-automation-id^="formField"]');
    if (wdc) {
      const l = wdc.querySelector('label, legend, [data-automation-id="formLabel"]');
      if (l && textOf(l)) return [textOf(l), 'workday-container'];
    }
    let node = el;
    for (let depth = 0; depth < 5 && node; depth++) {
      let sib = node.previousElementSibling;
      while (sib) {
        if (!['SCRIPT','STYLE','INPUT','SELECT','TEXTAREA','BUTTON'].includes(sib.tagName)) {
          const t = textOf(sib);
          if (t && t.length < 250) return [t, 'nearest-text'];
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
      if (node && node.tagName === 'FIELDSET') {
        const lg = node.querySelector(':scope > legend');
        if (lg && textOf(lg)) return [textOf(lg), 'legend'];
      }
      if (node && node.tagName === 'FORM') break;
    }
    return null;
  };
  const labelFor = (el, root) => {
    return ownLabelText(el, root)
      || (el.getAttribute('placeholder') ? [clean(el.getAttribute('placeholder')), 'placeholder'] : null)
      || nearestText(el)
      || (el.getAttribute('title') ? [clean(el.getAttribute('title')), 'title'] : null)
      || [el.name || el.id || '', 'attribute'];
  };
  const groupLabel = (el, root) => {
    const grp = el.closest('[role="radiogroup"], [role="group"], fieldset, [data-automation-id^="formField"]');
    if (grp) {
      const lb = grp.getAttribute('aria-labelledby');
      if (lb) { const t = lb.split(/\s+/).map(i => textOf(findById(root, i))).filter(Boolean).join(' '); if (t) return [t, 'aria-labelledby']; }
      if (grp.getAttribute('aria-label')) return [clean(grp.getAttribute('aria-label')), 'aria-label'];
      const lg = grp.querySelector(':scope > legend, :scope > label, :scope > [data-automation-id="formLabel"], :scope > div > legend');
      if (lg && textOf(lg)) return [textOf(lg), grp.tagName === 'FIELDSET' ? 'legend' : 'group-label'];
      return nearestText(grp) || ['', 'none'];
    }
    return nearestText(el) || ['', 'none'];
  };
  const isRequired = (el, label) => !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label || ''));
  const autoId = el => el.getAttribute('data-automation-id') || null;
  const containerAuto = el => { const c = el.parentElement && el.parentElement.closest('[data-automation-id]'); return c ? c.getAttribute('data-automation-id') : null; };

  const fields = [];
  const groups = {};
  const seen = new Set();
  for (const [root, inShadow] of roots) {
    const controls = root.querySelectorAll('input, select, textarea, button[aria-haspopup="listbox"], [role="combobox"]:not(input)');
    for (const el of controls) {
      if (seen.has(el)) continue; seen.add(el);
      const tag = el.tagName.toLowerCase();
      let type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : 'text')).toLowerCase();
      if (['hidden','submit','button','reset','image','search'].includes(type) && tag === 'input') continue;
      if (tag === 'button' || (el.getAttribute('role') === 'combobox' && tag !== 'input' && tag !== 'select')) type = 'custom_select';
      const visible = isVisible(el);
      if (!visible && type !== 'file') continue;  // file inputs are often visually hidden behind a styled button
      if (el.disabled) continue;
      if (type === 'radio' || (type === 'checkbox' && el.name && root.querySelectorAll('input[type="checkbox"][name="' + CSS.escape(el.name) + '"]').length > 1)) {
        const key = (type === 'radio' ? 'r:' : 'c:') + (el.name || stamp(el));
        const [ol] = ownLabelText(el, root) || [el.value || '', 'value'];
        if (!groups[key]) {
          const [gl, gsrc] = groupLabel(el, root);
          groups[key] = { jb_id: stamp(el), selector: sel(el), tag: 'input', type: type === 'radio' ? 'radio' : 'checkbox_group',
            label: gl, label_source: gsrc, name: el.name || null, html_id: el.id || null, automation_id: autoId(el),
            container_automation_id: containerAuto(el), placeholder: null, required: isRequired(el, gl), options: [],
            accept: null, multiple: type !== 'radio', max_length: null, current_value: null, in_shadow: inShadow };
          fields.push(groups[key]);
        }
        groups[key].options.push({ value: el.value, label: ol, selector: sel(el) });
        if (el.required) groups[key].required = true;
        if (el.checked) groups[key].current_value = el.value;
        continue;
      }
      const [label, src] = labelFor(el, root);
      let options = [];
      if (tag === 'select') {
        options = Array.from(el.options).map(o => ({ value: o.value, label: clean(o.textContent), selector: null }));
      } else if (type === 'custom_select') {
        const lbId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
        const lb = findById(root, lbId);
        if (lb) options = Array.from(lb.querySelectorAll('[role="option"]')).map(o => ({ value: o.getAttribute('data-value') || null, label: textOf(o) || clean(o.textContent), selector: null }));
      }
      fields.push({ jb_id: stamp(el), selector: sel(el), tag, type, label, label_source: src, name: el.getAttribute('name'),
        html_id: el.id || null, automation_id: autoId(el), container_automation_id: containerAuto(el),
        placeholder: el.getAttribute('placeholder'), required: isRequired(el, label), options,
        accept: el.getAttribute('accept'), multiple: !!el.multiple,
        max_length: el.maxLength && el.maxLength > 0 ? el.maxLength : null,
        current_value: type === 'checkbox' ? el.checked : (type === 'file' ? null : (tag === 'button' ? clean(el.textContent) : el.value)),
        in_shadow: inShadow });
    }
  }
  const buttons = [];
  for (const [root] of roots) {
    for (const b of root.querySelectorAll('button, input[type="submit"], input[type="button"], a[role="button"], [role="button"]')) {
      if (b.getAttribute('aria-haspopup') === 'listbox' || !isVisible(b)) continue;
      const text = clean(b.tagName === 'INPUT' ? b.value : (b.innerText || b.textContent || b.getAttribute('aria-label') || ''));
      buttons.push({ text, jb_id: stamp(b), selector: sel(b), automation_id: autoId(b), type: b.getAttribute('type'), disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true' });
    }
  }
  const errors = [];
  for (const [root] of roots) {
    for (const e of root.querySelectorAll('[data-automation-id="errorMessage"], [role="alert"], .error, .field-error, .error-message, [aria-invalid="true"] ~ .error')) {
      if (isVisible(e) && textOf(e)) errors.push(textOf(e));
    }
  }
  return { url: location.href, title: document.title, fields, buttons, errors };
}
"""

REQ_STAR = re.compile(r"\s*\*+\s*$")
SUBMIT_TEXT = re.compile(r"^(submit( application| my application)?|send application|apply( now)?|finish|complete application)$", re.I)
NEXT_TEXT = re.compile(r"^(next|continue|save (and|&) continue|next step|review|proceed)\b", re.I)
BACK_TEXT = re.compile(r"^(back|previous|go back)\b", re.I)


def classify_button(b: Button, submit_ids: set[str] | None = None, next_ids: set[str] | None = None) -> str:
    """Text wins over automation ids: Workday reuses its 'next' button id for the final Submit."""
    t = (b.text or "").strip()
    if SUBMIT_TEXT.match(t):
        return "submit"
    if NEXT_TEXT.match(t):
        return "next"
    if BACK_TEXT.match(t):
        return "back"
    if b.automation_id and submit_ids and b.automation_id in submit_ids:
        return "submit"
    if b.automation_id and next_ids and b.automation_id in next_ids:
        return "next"
    if b.type == "submit" and not t:
        return "submit"
    return "other"


def _field_id(raw: dict[str, Any], frame_index: int) -> str:
    if raw.get("type") in ("radio", "checkbox_group"):  # groups: the shared name identifies the question
        key = raw.get("name") or raw.get("container_automation_id") or raw["jb_id"]
    else:
        key = raw.get("automation_id") or raw.get("name") or raw.get("html_id") or raw["jb_id"]
    return f"f{frame_index}:{key}" if frame_index else key


def _to_field(raw: dict[str, Any], frame_index: int, frame_url: str) -> FormField:
    label = REQ_STAR.sub("", raw.get("label") or "").strip()
    required = bool(raw.get("required"))
    ftype = raw.get("type") or "text"
    known = {"text", "email", "tel", "url", "number", "date", "textarea", "select", "custom_select", "radio",
             "checkbox", "checkbox_group", "file", "password"}
    if ftype not in known:
        ftype = "text" if raw.get("tag") == "input" else "unknown"
    return FormField(
        field_id=_field_id(raw, frame_index), jb_id=raw["jb_id"], frame_index=frame_index, frame_url=frame_url,
        selector=raw["selector"], tag=raw.get("tag") or "input", type=ftype, label=label,
        label_source=raw.get("label_source") or "none", name=raw.get("name"), html_id=raw.get("html_id"),
        automation_id=raw.get("automation_id"), container_automation_id=raw.get("container_automation_id"),
        placeholder=raw.get("placeholder"), required=required,
        options=[Option(**o) for o in raw.get("options") or [] if (o.get("label") or o.get("value"))],
        accept=raw.get("accept"), multiple=bool(raw.get("multiple")), max_length=raw.get("max_length"),
        current_value=raw.get("current_value"), in_shadow=bool(raw.get("in_shadow")),
    )


async def inspect_page(page: Any, submit_ids: set[str] | None = None, next_ids: set[str] | None = None) -> PageSnapshot:
    """Inspect the main frame and every child frame (Greenhouse embeds its form in an iframe)."""
    snap = PageSnapshot(url=page.url, title=await page.title())
    for idx, frame in enumerate(page.frames):
        try:
            raw = await frame.evaluate(EXTRACT_JS, idx)
        except Exception as exc:  # detached / cross-origin issues
            log.debug("frame %d not inspectable: %s", idx, type(exc).__name__)
            continue
        snap.fields.extend(_to_field(f, idx, frame.url) for f in raw["fields"])
        for b in raw["buttons"]:
            btn = Button(frame_index=idx, **b)
            btn.kind = classify_button(btn, submit_ids, next_ids)  # type: ignore[assignment]
            snap.buttons.append(btn)
        snap.error_texts.extend(raw.get("errors") or [])
    # De-duplicate field ids (e.g. two inputs sharing a name across frames)
    seen: dict[str, int] = {}
    for f in snap.fields:
        if f.field_id in seen:
            seen[f.field_id] += 1
            f.field_id = f"{f.field_id}#{seen[f.field_id]}"
        else:
            seen[f.field_id] = 0
    return snap


def frame_for(page: Any, frame_index: int) -> Any:
    frames = page.frames
    return frames[frame_index] if frame_index < len(frames) else page.main_frame
