# Form handling

How CI fills application forms deterministically, what it does when it can't, and why it always stops
before the final Submit. No LLM runs in CI. Anything CI can't fill becomes a question for the on-box
agent (`field_questions`) or for Mike (the approval card).

## 1. Loop (per application in status `tailored`)

```
goto apply_url
repeat (max_steps = 12):
  detect blockers (CAPTCHA / login wall / one-time code / rate limit) ─► pause + Telegram alert, stop
  inspect page (all frames, open shadow roots)  ─► fields + buttons
  map fields (layers below)  ─► fill with human-paced typing/clicks
  if a validation error repeats on the same page ─► stop (questions or pause)
  if only Submit remains ─► screenshot, STOP (unless a valid ApprovalToken is present)
  else click Next / Continue / Save and Continue
```

After the loop, `pipeline.queue_field_questions` decides the outcome:

| Situation | Result |
|---|---|
| New agent-answerable fields were left empty (see §4) | `field_questions` rows (`pending`), app → `needs_answers`, screenshot stored (tag `needs_answers`); no Telegram card yet |
| Everything else | app → `filled` → `awaiting_approval`, screenshot in DB, Telegram card listing the low-confidence fields |
| Blocker (CAPTCHA, login, code) | app → `paused`, Telegram alert with screenshot. jobbuddy never solves or bypasses these |
| Crash or no navigation found | app → `failed` |

When the agent has answered or dismissed all of the questions, the app goes back to `tailored` and the
next CI run refills the form from scratch (`run-ci` never keeps browser state between runs).

## 2. Inspector (`engine/form_inspector.py`)

* Runs `EXTRACT_JS` in **every frame** (Greenhouse embeds its form in an iframe). It recurses into **open
  shadow roots** and stamps each control with `data-jb-id`, so field ids are stable within a page state.
  Ids carry a frame prefix (`f<frame index>-`).
* The label is the first non-empty source in this order: `label[for]` → `aria-labelledby` → wrapping
  `<label>` → `aria-label` → `placeholder` → Workday `formField-*` container label → nearest preceding
  text → `title` → `name`/`id`.
* Types recognized: text, email, tel, url, number, date, textarea, native `select`, custom listboxes
  (Workday `button[aria-haspopup=listbox]`, and `input[role=combobox]` such as Greenhouse's react-select
  dropdowns and location autocomplete), radio groups and checkbox groups (grouped by `name`), single
  checkbox, and file. Each field records `required` (attribute, `aria-required`, or `*` in the label),
  `max_length`, and options.
* Inputs marked `aria-hidden="true"` with `tabindex="-1"` (Greenhouse's hidden "requiredInput" next to
  each dropdown, whose nearest text is the "Select..." placeholder) are proxies, not questions, and are skipped.
* Combobox inputs (`input[role=combobox]`) render options only while open, so the filler discovers them at
  fill time: it types the value, picks the best rendered option (exact → normalized → yes/no synonyms →
  token overlap; if the typed text filters everything out it clears the filter and matches against the full
  list), then clicks the option by role and exact accessible name. Enter is only a fallback when that
  option is visible and first. Typed text alone is never trusted (react-select drops it on blur). If no
  option matches, the field is cleared and listed on the approval card with the options as suggestions.
* Buttons are classified as submit, next, or other. Button text wins over automation ids, so a Workday
  "Next" button that turns into "Submit" on the last page is treated as Submit.

## 3. Mapper layers (`engine/field_mapper.py`)

| Order | Layer | Confidence |
|---|---|---|
| 1 | Human override (Telegram/dashboard/CLI edit, by `field_id` or normalized label) | 1.00 |
| 2 | ATS adapter key (Workday `data-automation-id`, Greenhouse/Lever/Ashby names) | 0.98 |
| 3 | Learned mapping (`form_field_mappings` from human edits and reusable agent answers) | stored (agent: 0.85) |
| 4 | Label regex rules (name, email, phone, LinkedIn, location, how-did-you-hear first, …) | 0.90 × option-match score |
| 5 | On-box agent answer (`field_questions` answered for this application, by `field_id` then label) | 0.85 × option-match score |
| 6 | Open-ended question answered at tailoring time (`applications.answers`, matched by normalized label) | 0.85 |
| 7 | Optional model fallback (library only; CI always passes `llm=None`) | capped at 0.80 |

* Values for `select`, radio, and listbox fields must match an option. Matching runs exact → normalized →
  yes/no synonyms → token overlap (≥ 0.6), otherwise the field is left empty.
* **Never filled automatically:** password fields; sensitive questions (work authorization, sponsorship,
  salary, start date, relocation, EEO, criminal history), which use only explicit, non-TODO
  `profile.application_answers` values or Mike; any value still marked `TODO` in `profile.yaml`; and
  consent checkboxes, which are pre-ticked at 0.6 only so they are flagged for Mike's review.
* Fields below `min_confidence_autofill` (0.75), and empty required fields, are listed on the approval card
  with suggestions.

## 4. What becomes a `field_question` for the agent

A field is queued only if all of these hold: it is empty after all layers; it has a label; it is not a
file, password, or hidden field; it is not sensitive (`tailoring.questions.SENSITIVE_PATTERNS`); its gap
is not driven by the profile (a TODO or missing profile key is Mike's to fix); it is not a consent or
resume-paste field; and it is required, or it is an open-ended textarea (label of 12+ characters). A
field that already has a row for this application (pending, answered, or dismissed) is never re-asked.

The agent answers by following `jobbuddy/prompts/answer_questions.md`. `validate-answers` checks that
choice answers are one of the options, that max length is respected, that no sensitive question is
answered, and runs the fabrication check (unknown numbers, tools, or proper nouns). The agent may mark
an answer `reusable`, which saves it to `form_field_mappings` without overwriting human-learned rows.

## 5. Files

The resume is the latest `tailored_resume` document for the application (rendered and uploaded by
`agent-push-tailoring`), falling back to the base `resume` document. For a cover-letter upload the filler
uses the agent's rendered file; if there isn't one, it renders `applications.cover_letter_text`, or a
deterministic template from profile facts. Files are written to a temp dir only for the upload.

## 6. Stop before Submit; approvals

* `FormFiller.run()` clicks Submit **only** with an `ApprovalToken`. `engine/submitter.issue_token()` is
  the only place a token is created. It requires status `approved` and an unconsumed `approve` row in
  `approvals`, consumes that row atomically (`UPDATE … WHERE consumed_at IS NULL`), and commits before any
  click. One approval allows at most one submission attempt.
* Before clicking, the filler refuses if any required field is still empty. After the click it checks
  for blockers and for an ATS confirmation pattern. If the outcome is unclear the app is marked `failed`
  and is never retried automatically, because the click may have gone through.
* Telegram callbacks are accepted only from `TELEGRAM_CHAT_ID` **and** `TELEGRAM_ALLOWED_USER_ID`, and only
  with the application's current single-use nonce. Each edit triggers a refill and a fresh card with a new
  nonce.
* CI submits only if the repository variable `AUTO_SUBMIT=true`. Otherwise Mike runs `jobbuddy submit --confirm`.

## 7. Workday specifics

The 4-step wizard (My Information → My Experience → Application Questions → Voluntary Disclosures →
Review) is handled by the same loop. The adapter maps Workday `data-automation-id` keys. Custom listboxes
are opened and the option is clicked by its text. The "Sign In / Create Account" wall is a blocker:
jobbuddy never creates accounts or logs in.

## 8. Human pacing

`engine/human.py` adds random per-keystroke delays (45-140 ms) and action pauses (350-1200 ms). These keep
load on employer sites polite. They are **not** an anti-bot evasion technique: jobbuddy has no stealth
plugins and does no fingerprint spoofing.

## Radio-like button pairs, the required-field safety net, and submit outcomes

* **Yes/No button pairs** (Ashby `ashby-application-form-input-yesno`: `<button aria-pressed data-option>` siblings
  plus a visually hidden checkbox) are reported as `type: radio`, `tag: button`; the filler clicks the matching
  option unless it is already `aria-pressed="true"`. The hidden checkbox is not reported separately.
* **Safety net:** right before stopping at Submit the filler re-inspects the page. Every required field whose
  value is still empty, and every required question label with no control we recognise
  (`PageSnapshot.unhandled_required`), goes to the top of `low_confidence` (`source: "verify"`) and into
  `FillResult.required_empty`. The approval card opens with a "REQUIRED field(s) still EMPTY" warning, and
  `_submit` refuses (paused) while any remain.
* **Submit outcome:** after clicking Submit the filler waits up to `engine.submit_outcome_timeout_seconds` (45 s)
  for a confirmation, a challenge, a security-code prompt (Greenhouse answers `captcha-failed` and emails an
  8-character code when its invisible reCAPTCHA Enterprise score is too low), field errors, or the application
  POST response (status + JSON `code` only). `likely_submitted` is True / False / None and the Telegram message
  says plainly "NOT submitted" (with the job link to apply manually) or "outcome UNCLEAR, check your email".
  jobbuddy never retries a submit.
