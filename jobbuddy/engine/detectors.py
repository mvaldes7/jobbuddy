"""Detect anything that needs a human: CAPTCHA, login/account walls, email/SMS codes.

jobbuddy NEVER attempts to solve, bypass, or outsource CAPTCHAs. Detection => pause + alert Mike.
"""
from __future__ import annotations

from typing import Any

from jobbuddy.engine.models import Blocker


DETECT_JS = r"""
() => {
  const vis = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect(); return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0); };
  const q = s => Array.from(document.querySelectorAll(s));
  const out = { captcha: [], login: [], code: [] };
  for (const el of q('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #captcha, [id*="captcha" i], [class*="captcha" i], iframe[title*="captcha" i], iframe[src*="captcha" i], iframe[src*="challenges.cloudflare.com"], iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], iframe[src*="geetest"]')) {
    // Invisible reCAPTCHA v3 / Enterprise badges are not challenges; a visible widget is.
    if (!vis(el) || el.closest('.grecaptcha-badge') || el.classList.contains('grecaptcha-badge') || /size=invisible/.test(el.getAttribute('src') || '')) continue;
    out.captcha.push((el.tagName + ' ' + (el.className || el.id || el.getAttribute('title') || '')).slice(0, 80));
  }
  const pw = q('input[type="password"]').filter(vis);
  if (pw.length) out.login.push('visible password field');
  for (const id of ['signInContent', 'signInSubmitButton', 'createAccountSubmitButton', 'createAccountLink', 'utilityButtonSignIn']) {
    const el = document.querySelector('[data-automation-id="' + id + '"]');
    if (el && vis(el)) out.login.push('workday ' + id);
  }
  const otp = q('input[autocomplete="one-time-code"]').filter(vis);
  if (otp.length) out.code.push('one-time-code input');
  const body = (document.body && document.body.innerText || '').slice(0, 20000).toLowerCase();
  if (/verify (that )?you('| a)re (a )?human|are you a robot|complete the security check/.test(body)) out.captcha.push('human-verification text');
  if (/(enter|type) the (verification |security |6-digit )?code (we|that we) (sent|emailed|texted)|check your email for a (verification )?code/.test(body)) out.code.push('verification-code text');
  if (/too many requests|rate limit exceeded|access denied/.test(body)) out.rate = 'rate-limit text';
  return out;
}
"""


async def detect_blockers(page: Any) -> list[Blocker]:
    blockers: list[Blocker] = []
    for frame in page.frames:
        # Captcha iframes are detected from the PARENT document (element visibility matters:
        # reCAPTCHA keeps a hidden challenge frame around even when nothing is being asked).
        try:
            res = await frame.evaluate(DETECT_JS)
        except Exception:
            continue
        blockers += [Blocker(kind="captcha", evidence=e) for e in res.get("captcha", [])]
        blockers += [Blocker(kind="login", evidence=e) for e in res.get("login", [])]
        blockers += [Blocker(kind="email_code", evidence=e) for e in res.get("code", [])]
        if res.get("rate"):
            blockers.append(Blocker(kind="rate_limited", evidence=res["rate"]))
    uniq: dict[tuple[str, str], Blocker] = {(b.kind, b.evidence): b for b in blockers}
    return list(uniq.values())
