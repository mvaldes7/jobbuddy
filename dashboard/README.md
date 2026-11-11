# jobbuddy dashboard

Small Streamlit app over the jobbuddy database: overview counts, the matches sent to you on Telegram
(with cover letter PDFs), outcome tracking, and recent scores. It never submits anything; its only
write is an INSERT into the append-only `application_events` table.

## Deploy on Streamlit Community Cloud

1. Sign in at <https://share.streamlit.io> with GitHub and allow access to `mvaldes7/jobbuddy` (private repo).
2. **Create app → Deploy a public app from GitHub** (the app itself is gated by `DASHBOARD_PASSWORD`):
   - Repository: `mvaldes7/jobbuddy`
   - Branch: `main`
   - Main file path: `dashboard/app.py`
3. **Advanced settings**:
   - Python version: `3.12`
   - Secrets (TOML):
     ```toml
     DATABASE_URL = "postgresql://jobbuddy_dashboard:<password>@<host>-pooler.<region>.aws.neon.tech/<db>?sslmode=require"
     DASHBOARD_PASSWORD = "<choose a password>"
     ```
     Copy the pooled connection string for role `jobbuddy_dashboard` from the Neon console (Connect).
     `postgres://` / `postgresql://` are both fine; the app switches to psycopg, drops
     `channel_binding`, and keeps `sslmode=require`.
4. Deploy. Dependencies come from `dashboard/requirements.txt` (next to the main file).

Never commit secrets: `.streamlit/secrets.toml` is git-ignored.

## Run locally

```bash
pip install -r dashboard/requirements.txt
DATABASE_URL=sqlite:///./jobbuddy.db streamlit run dashboard/app.py   # or put keys in .streamlit/secrets.toml
```

## Database role

`jobbuddy_dashboard` needs SELECT on `companies, jobs, job_scores, applications, application_events`
(plus `candidate_documents` for cover letter downloads; without it the download is hidden) and
INSERT on `application_events` (+ `USAGE, SELECT` on `application_events_id_seq`). Nothing else.

Outcomes use the same values as `jobbuddy outcome` (`none, interview, rejected, offer, ghosted, withdrawn`);
the pipeline's reconcile step copies them onto `applications.outcome`. "I applied" writes an
`applied_by_user` event that is informational only.
