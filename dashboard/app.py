"""jobbuddy dashboard (Streamlit). Deployable to Azure App Service F1.

Security model:
  * Connects with its OWN role via DATABASE_URL (e.g. jobbuddy_dashboard).
  * Writes ONLY: INSERT into `approvals` (approve/edit/skip) and INSERT into `application_events`
    (manual outcome reports: interview/rejected/offer/...). It never UPDATEs applications;
    the pipeline reconciles statuses from those rows on its next run.
  * No Telegram token or other bot secrets here.
  * Optional password gate via DASHBOARD_PASSWORD (also put App Service Authentication in front).
Run locally:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

OUTCOMES = ["none", "interview", "rejected", "offer", "ghosted", "withdrawn"]
FUNNEL = ["found", "filtered", "scored", "tailored", "filled", "awaiting_approval", "approved", "submitted"]


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./jobbuddy.db")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return url


@st.cache_resource
def engine():
    url = _url()
    if url.startswith("postgresql"):
        return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1, pool_recycle=300,
                             connect_args={"prepare_threshold": None})
    return create_engine(url, pool_pre_ping=True)


def is_pg() -> bool:
    return engine().dialect.name == "postgresql"


def q(sql: str, **params) -> pd.DataFrame:
    with engine().connect() as c:
        return pd.read_sql(text(sql), c, params=params)


def _json_param(name: str) -> str:
    return f"CAST(:{name} AS JSONB)" if is_pg() else f":{name}"


def insert_approval(app_id: int, decision: str, edits: dict | None = None) -> None:
    """INSERT-only write path #1."""
    with engine().begin() as c:
        c.execute(text(f"INSERT INTO approvals (application_id, nonce, decision, decided_via, edits, decided_at) "
                       f"VALUES (:a, :n, :d, 'dashboard', {_json_param('e')}, :t)"),
                  {"a": app_id, "n": secrets.token_urlsafe(18), "d": decision,
                   "e": json.dumps(edits) if edits else None, "t": datetime.now(timezone.utc)})


def insert_outcome_event(app_id: int, outcome: str) -> None:
    """INSERT-only write path #2 (append-only audit log)."""
    with engine().begin() as c:
        c.execute(text(f"INSERT INTO application_events (application_id, event_type, actor, note, payload, created_at) "
                       f"VALUES (:a, 'outcome_reported', 'dashboard', :note, {_json_param('p')}, :t)"),
                  {"a": app_id, "note": f"outcome -> {outcome}", "p": json.dumps({"outcome": outcome}),
                   "t": datetime.now(timezone.utc)})


def gate() -> bool:
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw:
        return True
    if st.session_state.get("authed"):
        return True
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered, pw):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    return False


def funnel_view() -> None:
    st.subheader("Pipeline funnel")
    jobs = q("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    apps = q("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
    counts = {r.status: int(r.n) for r in jobs.itertuples()}
    acounts = {r.status: int(r.n) for r in apps.itertuples()}
    total_jobs = sum(counts.values())
    passed = total_jobs - counts.get("filtered_out", 0) - counts.get("closed", 0)
    stage_vals = {
        "found": total_jobs, "filtered": passed,
        "scored": counts.get("scored", 0),
    }
    order = ["tailored", "filled", "awaiting_approval", "approved", "submitted"]
    for i, s in enumerate(order):  # cumulative: an app in a later stage passed the earlier ones
        stage_vals[s] = sum(acounts.get(x, 0) for x in order[i:])
    df = pd.DataFrame({"stage": FUNNEL, "count": [stage_vals.get(s, 0) for s in FUNNEL]})
    st.bar_chart(df.set_index("stage"))
    c1, c2, c3 = st.columns(3)
    c1.metric("Jobs tracked", total_jobs)
    c2.metric("Awaiting approval", acounts.get("awaiting_approval", 0))
    c3.metric("Paused (needs you)", acounts.get("paused", 0))
    st.caption("On-box agent queues (read-only)")
    pending_q = q("SELECT COUNT(*) AS n FROM field_questions WHERE status = 'pending'")
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("To score (jobs 'filtered')", counts.get("filtered", 0))
    a2.metric("To tailor (apps 'scored')", acounts.get("scored", 0))
    a3.metric("Field questions pending", int(pending_q.n.iloc[0]) if not pending_q.empty else 0)
    a4.metric("Manual apply (Wellfound)", counts.get("manual_apply", 0))
    manual = q("""SELECT title, company_name AS company, location, url AS link FROM jobs
                  WHERE status = 'manual_apply' ORDER BY first_seen_at DESC LIMIT 50""")
    if not manual.empty:
        st.caption("Apply by hand (no public ATS posting found; jobbuddy never opens these links)")
        st.dataframe(manual, hide_index=True, column_config={"link": st.column_config.LinkColumn("link")})


def responses_view() -> None:
    st.subheader("Response rate (submitted applications)")
    df = q("SELECT outcome, COUNT(*) AS n FROM applications WHERE submitted_at IS NOT NULL GROUP BY outcome")
    total = int(df.n.sum()) if not df.empty else 0
    by = {r.outcome: int(r.n) for r in df.itertuples()}
    responded = by.get("interview", 0) + by.get("rejected", 0) + by.get("offer", 0)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Submitted", total)
    c2.metric("Response rate", f"{responded / total:.0%}" if total else "-")
    c3.metric("Interviews", by.get("interview", 0))
    c4.metric("Offers", by.get("offer", 0))


def approval_queue() -> None:
    st.subheader("Approval queue")
    df = q("""SELECT a.id, j.title, j.company_name, j.url, a.low_confidence, a.cover_letter_text, a.updated_at
              FROM applications a JOIN jobs j ON j.id = a.job_id
              WHERE a.status = 'awaiting_approval' ORDER BY a.updated_at""")
    pending = q("SELECT application_id, decision FROM approvals WHERE processed_at IS NULL")
    pend = {int(r.application_id): r.decision for r in pending.itertuples()}
    if df.empty:
        st.info("Nothing awaiting approval.")
        return
    for r in df.itertuples():
        with st.expander(f"#{r.id} {r.title} @ {r.company_name}"):
            if r.url:
                st.markdown(f"[Posting]({r.url})")
            if int(r.id) in pend:
                st.warning(f"Decision '{pend[int(r.id)]}' recorded; applied on the next pipeline run.")
            try:
                shot = q("SELECT png FROM screenshots WHERE application_id = :a ORDER BY id DESC LIMIT 1", a=int(r.id))
                if not shot.empty:
                    st.image(bytes(shot.png.iloc[0]), caption="Filled form (stopped before Submit)")
            except Exception:
                st.caption("Screenshot not visible to this role.")
            lc = r.low_confidence
            if isinstance(lc, str):
                lc = json.loads(lc or "[]")
            if lc:
                st.write("Fields needing review")
                st.dataframe(pd.DataFrame(lc)[["field_id", "label", "value", "confidence", "reason", "suggestions"]],
                             use_container_width=True)
            if r.cover_letter_text:
                st.text_area("Cover letter", r.cover_letter_text, height=220, disabled=True, key=f"cl{r.id}")
            edits_raw = st.text_area("Edits (one per line: field_id = value)", key=f"ed{r.id}")
            c1, c2, c3 = st.columns(3)
            if c1.button("Approve", key=f"ap{r.id}", type="primary"):
                insert_approval(int(r.id), "approve")
                st.success("Approval recorded")
            if c2.button("Send edits", key=f"e{r.id}"):
                edits = {}
                for line in edits_raw.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        edits[k.strip()] = v.strip()
                if edits:
                    insert_approval(int(r.id), "edit", edits)
                    st.success("Edits recorded; the form is re-filled and re-sent for approval")
            if c3.button("Skip", key=f"sk{r.id}"):
                insert_approval(int(r.id), "skip")
                st.success("Skip recorded")


def applications_table() -> None:
    st.subheader("Applications")
    df = q("""SELECT a.id, j.title, j.company_name AS company, a.status, a.outcome, j.url AS link,
                     a.submitted_at, a.pause_reason
              FROM applications a JOIN jobs j ON j.id = a.job_id ORDER BY a.id DESC""")
    status = st.multiselect("Status filter", sorted(df.status.unique()) if not df.empty else [])
    if status:
        df = df[df.status.isin(status)]
    st.dataframe(df, use_container_width=True, column_config={"link": st.column_config.LinkColumn("link")})
    st.markdown("**Update outcome** (recorded as an event; applied by the pipeline)")
    c1, c2, c3 = st.columns([1, 1, 1])
    app_id = c1.number_input("Application id", min_value=1, step=1)
    outcome = c2.selectbox("Outcome", OUTCOMES[1:])
    if c3.button("Record outcome"):
        insert_outcome_event(int(app_id), outcome)
        st.success(f"Recorded #{int(app_id)} -> {outcome}")


def main() -> None:
    st.set_page_config(page_title="jobbuddy", layout="wide")
    st.title("jobbuddy")
    if not gate():
        return
    tab1, tab2, tab3 = st.tabs(["Overview", "Approval queue", "Applications"])
    with tab1:
        funnel_view()
        responses_view()
    with tab2:
        approval_queue()
    with tab3:
        applications_table()


main()
