# Safety, terms of service, and PII

jobbuddy is a personal assistant for one candidate (Mike). It is built so that the risky parts (choosing
where to apply, what to say, and clicking Submit) stay with Mike. The rules below are enforced in code
and tests wherever possible.

## 1. Platform terms of service

| Platform | What jobbuddy does | What it never does | Why |
|---|---|---|---|
| **LinkedIn** | Parses job-alert e-mails Mike already receives (direct or forwarded) and resolves each lead to the employer's own ATS posting | Requests linkedin.com pages, scrapes, logs in, or uses **Easy Apply** | The LinkedIn User Agreement prohibits scraping, bots, and automated access to its services, including automated applications. Accounts get restricted |
| **Indeed** | Same: alert e-mails only, resolved to the employer's ATS | Scrapes indeed.com or automates Indeed Apply | Indeed's Terms prohibit scraping and automated use of its site, and Indeed Apply is an Indeed-hosted flow |
| **Wellfound (AngelList Talent)** | Parses alert and digest e-mails (`*@wellfound.com`, `*@angel.co`, forwarded too) into leads (`source='wellfound'`: company, title, location/remote, salary and equity range, Wellfound link). Then looks for the same title on the company's **public** Ashby → Greenhouse → Lever board API. An unresolved lead becomes `manual_apply` and its Wellfound link is sent to Mike in the Telegram digest | Requests wellfound.com or angel.co (not even to follow the links in the e-mail), scrapes, logs in, or automates in-app apply | Wellfound has no public jobs API, protects its site against bots, and its terms disallow automated access. In-app apply sends messages under Mike's account, so only Mike does it |
| **Greenhouse, Lever, Ashby, BambooHR, Workday** | Reads the public job-board endpoints companies publish for exactly this purpose. Fills the public application form and stops before Submit | Creates accounts, signs in, bypasses CAPTCHAs, or submits without Mike's approval | These are the employer's own channels. A Workday "Sign In / Create Account" wall is treated as a blocker (pause + alert) |

The tests enforce this: `tests/test_email_alerts.py` mounts respx routes for linkedin.com, indeed.com,
wellfound.com, and angel.co, and asserts that **none of them is ever called** while alerts are ingested
and resolved. `ALERT_SOURCES` (linkedin_alert, indeed_alert, wellfound) are never filtered, filled, or
applied to by CI. Only their resolved ATS postings are.

## 2. Request behavior and rate limits

* There is one HTTP client (`fetchers/base.PoliteHttp`). It sends an honest User-Agent (`jobbuddy/0.1
  (personal job search)`), waits ≥ 1.5 s between requests to the same host, honors `429`/`Retry-After` with
  exponential backoff, and has a bounded retry count. There are no parallel floods.
* Volume caps: CI runs 5 times a day, at most 3 form fills per run, at most 5 submissions per day
  (`DAILY_APPLICATION_CAP`), and only for boards listed in `config/companies.yaml`.
* The browser paces typing and clicks at a human speed to keep load on employer sites low. jobbuddy uses
  **no stealth plugins and no fingerprint spoofing**, and it never hides that it is automation.
* **Residential vs datacenter IPs.** GitHub-hosted runners use datacenter (Azure) IP ranges. Public ATS
  APIs are fine from there, but some application pages show more CAPTCHAs or blocks to datacenter IPs.
  jobbuddy does **not** use residential proxies, proxy rotation, or any other way to disguise where its
  traffic comes from. When a site challenges the runner, the application is paused and Mike gets an
  alert. He can then run `jobbuddy fill --app <id> --headed` from his own machine (his own residential
  connection, his own hands) or apply manually. The on-box agent only talks to Neon over HTTPS; it never
  browses job sites.

## 3. CAPTCHAs, logins, and codes

`engine/detectors.py` detects visible reCAPTCHA, hCaptcha, Cloudflare Turnstile, Arkose/FunCaptcha,
GeeTest, login walls, one-time-code inputs, and rate-limit pages. On detection jobbuddy **stops**: the
application goes to `paused`, a screenshot is stored, and Mike gets an alert. jobbuddy never solves,
bypasses, or outsources a CAPTCHA, never uses solver services, and never reads verification e-mail or
SMS codes.

## 4. Submission control

* `AUTO_SUBMIT` defaults to `false`. In CI it comes from a repository variable (unset means `false`).
* Every submission needs an `approvals` row with decision `approve`. That row is accepted only from the
  configured Telegram chat **and** user, with the application's current single-use nonce (or from the
  dashboard/CLI). It is consumed atomically right before the click, so one approval allows one attempt.
* The filler refuses to click Submit without an `ApprovalToken` and refuses if a required field is
  empty. If the outcome is unclear the app is marked `failed`, never retried.
* Content guardrails: tailoring may only reorder and reword existing bullets. `ClaimValidator` rejects
  any new numbers, tools, employers, titles, or skills that aren't in the profile, and any push with an
  error is rejected whole. The agent never answers sensitive questions (authorization, sponsorship,
  salary, EEO, criminal history, relocation, start date); those come only from Mike's explicit profile
  values.

## 5. No external LLM API, no mailbox credentials

CI makes no model calls (it asserts no `/chat/completions` requests in the e2e test). Scoring, tailoring,
cover letters, and answers are done by Mike's on-box agent, which reads only the minimum it needs
(`agent_profile()` omits e-mail, phone, address, and links). jobbuddy holds no IMAP credentials: the
agent passes alert e-mails it already has access to into `ingest-alert-email`.

## 6. PII handling

| Data | Where it lives | Protection |
|---|---|---|
| `profile/profile.yaml`, `resume.pdf`, `resume_or_cl.docx`, `cover_letter_sample.txt` | Mike's machine only | git-ignored and excluded from any zip/package. CI gets the profile from the `CANDIDATE_PROFILE_JSON` secret |
| Resume, voice sample, tailored documents, screenshots | Neon (`candidate_documents`, `screenshots`, bytea) + Mike's private Telegram chat | **never** uploaded as GitHub artifacts. The workflow has no `upload-artifact` step |
| Logs | GitHub Actions logs, local stdout | `logging_utils.PIIRedactingFilter` masks e-mails, phone numbers, bot/API tokens, URL credentials, and profile values. `run.yml` emits `::add-mask::` for every profile string before anything else runs |
| Database credentials | GitHub secrets, App Service settings, the box `.env` (git-ignored) | the Neon HTTP client never logs the connection string and redacts it from errors. `repr()` hides it |
| Voice sample | DB / local file | not committed in `prompts/cover_letter.md`. It's injected into the instructions only at `agent-pull` time |
| Agent work files | temp files on the box | `agent-push-tailoring` renders documents into a temp dir and deletes them after upload. The agent's pull/push JSON files should be deleted after each run |

Access is least-privilege. `jobbuddy_app` (CI and agent) is DML-only. `jobbuddy_dashboard` is read-only
(including `candidate_documents` for cover-letter downloads) and can INSERT only into `application_events`. It has no
`neon_superuser` membership. Only `migrate.yml` holds the owner URL
(`DATABASE_URL_DIRECT`). `application_events` is append-only, enforced by DB triggers.

## 7. Before you turn on `AUTO_SUBMIT`

1. Fill every `TODO` in `profile.yaml` (`jobbuddy check-profile`), including reviewing EEO answers and
   confirming target titles.
2. Watch at least a few Telegram cards end to end and approve or skip by hand.
3. Keep `DAILY_APPLICATION_CAP` low. Check `jobbuddy report` and the dashboard for paused or failed
   applications.
