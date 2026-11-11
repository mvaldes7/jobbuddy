"""jobbuddy dashboard (Streamlit). Deploys on Streamlit Community Cloud (main file: dashboard/app.py).

Current flow: jobs found -> filtered (CI) -> scored (on-box agent) -> application tailored ->
sent_to_user (Telegram listing + cover letter PDF). Mike applies by hand; nothing here submits anything.

Security model:
  * Connects with its OWN least-privilege role via DATABASE_URL (jobbuddy_dashboard).
  * The ONLY write is INSERT into `application_events` (append-only audit log):
      - event_type 'outcome_reported' + payload {"outcome": ...}: the same row `jobbuddy outcome` writes
        (repository.report_outcome); the pipeline's reconcile() copies it into applications.outcome.
      - event_type 'applied_by_user': Mike's note that he applied by hand. Informational only;
        reconcile() ignores it and applications.status stays 'sent_to_user'.
    It never UPDATEs or DELETEs anything and never touches `approvals`.
  * Settings come from st.secrets first, then environment variables: DATABASE_URL, DASHBOARD_PASSWORD.
  * No Telegram token or other bot secrets here. Secret values are never displayed.
Run locally:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# Mirrors jobbuddy.db.states.Outcome (what `jobbuddy outcome` accepts); tests/test_dashboard.py keeps them in sync.
OUTCOMES = ["none", "interview", "rejected", "offer", "ghosted", "withdrawn"]
OUTCOME_LABELS = {"none": "none (clear)", "interview": "interviewing", "rejected": "rejected", "offer": "offer",
                  "ghosted": "ghosted", "withdrawn": "withdrawn"}
APPLIED_EVENT = "applied_by_user"
DEFAULT_THRESHOLD = 65
LOCAL_TZ_NAME = "America/Los_Angeles"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


# ---------------- settings ----------------
def setting(name: str, default: str | None = None) -> str | None:
    """st.secrets first (Streamlit Cloud), then the environment."""
    try:
        if name in st.secrets:
            val = st.secrets[name]
            return str(val) if val is not None else default
    except Exception:  # no secrets.toml at all -> Streamlit raises; fall back to env
        pass
    return os.environ.get(name, default)


def normalize_url(url: str) -> str:
    """postgres:// | postgresql:// -> postgresql+psycopg://; drop channel_binding; ensure sslmode=require."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
            break
    if not url.startswith("postgresql"):
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "channel_binding"]
    if not any(k.lower() == "sslmode" for k, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@st.cache_resource
def engine():
    url = normalize_url(setting("DATABASE_URL") or "sqlite:///./jobbuddy.db")
    if url.startswith("postgresql"):
        # prepare_threshold=None: Neon's pooler (PgBouncer, transaction mode) can't use prepared statements.
        return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1, pool_recycle=300,
                             connect_args={"prepare_threshold": None})
    return create_engine(url, pool_pre_ping=True)


def is_pg() -> bool:
    return engine().dialect.name == "postgresql"


def q(sql: str, **params) -> pd.DataFrame:
    with engine().connect() as c:
        return pd.read_sql(text(sql), c, params=params)


@st.cache_data(ttl=3600)
def score_threshold() -> int:
    try:
        import yaml

        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return int(cfg["matching"]["agent_score_threshold"])
    except Exception:
        return DEFAULT_THRESHOLD


def local_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(LOCAL_TZ_NAME)
    except Exception:
        return timezone.utc


def to_local(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.tz_convert(local_tz())


def parse_json(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, (str, bytes)) and val:
        try:
            out = json.loads(val)
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}
    return {}


def reasons_text(val: Any) -> str:
    r = parse_json(val)
    bits = []
    for key in ("reasons", "strengths", "gaps"):
        items = r.get(key) or []
        if isinstance(items, list) and items:
            label = "" if key == "reasons" else f"{key}: "
            bits.append(label + "; ".join(str(i).strip() for i in items))
    for key in ("seniority_fit", "domain_fit"):
        if r.get(key):
            bits.append(f"{key.replace('_', ' ')}: {r[key]}")
    return " | ".join(bits)


# ---------------- the only write path ----------------
def _json_param(name: str) -> str:
    return f"CAST(:{name} AS JSONB)" if is_pg() else f":{name}"


def insert_event(app_id: int, event_type: str, note: str, payload: dict[str, Any]) -> None:
    """INSERT-only into the append-only application_events log (same columns repository.log_event fills)."""
    with engine().begin() as c:
        c.execute(text(f"INSERT INTO application_events (application_id, from_status, to_status, event_type, actor, "
                       f"note, payload, created_at) VALUES (:a, NULL, NULL, :et, 'dashboard', :note, "
                       f"{_json_param('p')}, :t)"),
                  {"a": int(app_id), "et": event_type, "note": note, "p": json.dumps(payload),
                   "t": datetime.now(timezone.utc)})


def insert_outcome_event(app_id: int, outcome: str) -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    insert_event(app_id, "outcome_reported", f"outcome -> {outcome}", {"outcome": outcome})


def insert_applied_event(app_id: int) -> None:
    insert_event(app_id, APPLIED_EVENT, "Mike applied by hand", {"applied": True})


# ---------------- auth ----------------
def gate() -> bool:
    pw = setting("DASHBOARD_PASSWORD")
    if not pw:
        return True
    if st.session_state.get("authed"):
        return True
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered.encode(), pw.encode()):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    return False


# ---------------- data ----------------
LATEST_SCORE = ("(SELECT s.llm_score FROM job_scores s WHERE s.job_id = j.id AND s.llm_score IS NOT NULL "
                "ORDER BY s.id DESC LIMIT 1)")


def sent_apps() -> pd.DataFrame:
    df = q(f"""SELECT a.id AS app_id, a.status, a.outcome, a.notified_at, a.updated_at,
                      j.company_name AS company, j.title, j.location,
                      COALESCE(j.url, j.apply_url) AS link, {LATEST_SCORE} AS score
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.status = 'sent_to_user'""")
    if df.empty:
        return df
    # Sent date: the transition into sent_to_user (fallback notified_at / updated_at).
    sent = q("""SELECT application_id AS app_id, MAX(created_at) AS sent_at FROM application_events
                WHERE event_type = 'transition' AND to_status = 'sent_to_user' GROUP BY application_id""")
    df = df.merge(sent, on="app_id", how="left")
    df["sent_at"] = to_local(df["sent_at"]).fillna(to_local(df["notified_at"])).fillna(to_local(df["updated_at"]))
    # Current outcome: newest dashboard/CLI report wins even before the pipeline reconciles it.
    evs = q("""SELECT application_id AS app_id, event_type, payload, created_at FROM application_events
               WHERE event_type IN ('outcome_reported', :applied) ORDER BY id""", applied=APPLIED_EVENT)
    reported: dict[int, str] = {}
    applied_at: dict[int, Any] = {}
    for r in evs.itertuples():
        if r.event_type == "outcome_reported":
            o = parse_json(r.payload).get("outcome")
            if o in OUTCOMES:
                reported[int(r.app_id)] = o
        else:
            applied_at[int(r.app_id)] = r.created_at
    df["outcome"] = [reported.get(int(i), o) for i, o in zip(df.app_id, df.outcome)]
    df["applied"] = [int(i) in applied_at for i in df.app_id]
    df["applied_at"] = to_local(pd.Series([applied_at.get(int(i)) for i in df.app_id], dtype="object"))
    return df.sort_values("sent_at", ascending=False, na_position="last").reset_index(drop=True)


def cover_letters(app_ids: list[int]) -> pd.DataFrame | None:
    """Latest cover_letter PDF per application, or None if candidate_documents isn't readable."""
    if not app_ids:
        return pd.DataFrame(columns=["doc_id", "app_id", "filename"])
    ids = ",".join(str(int(i)) for i in app_ids)
    try:
        return q(f"""SELECT d.id AS doc_id, d.application_id AS app_id, d.filename FROM candidate_documents d
                     WHERE d.kind = 'cover_letter' AND d.application_id IN ({ids})
                       AND d.id = (SELECT MAX(d2.id) FROM candidate_documents d2
                                   WHERE d2.kind = 'cover_letter' AND d2.application_id = d.application_id)""")
    except Exception:  # e.g. permission denied for the dashboard role
        return None


def document_bytes(doc_id: int) -> bytes | None:
    try:
        df = q("SELECT content FROM candidate_documents WHERE id = :i AND kind = 'cover_letter'", i=int(doc_id))
    except Exception:
        return None
    if df.empty:
        return None
    raw = df.content.iloc[0]
    return bytes(raw) if raw is not None else None


# ---------------- views ----------------
def overview_view(threshold: int) -> None:
    st.subheader("Overview")
    jobs = q("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    apps = q("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
    jc = {r.status: int(r.n) for r in jobs.itertuples()}
    ac = {r.status: int(r.n) for r in apps.itertuples()}
    sent = sent_apps()
    now = datetime.now(local_tz())
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last7 = now - timedelta(days=7)
    this_week = int((sent["sent_at"] >= week_start).sum()) if not sent.empty else 0
    in7 = int((sent["sent_at"] >= last7).sum()) if not sent.empty else 0

    c = st.columns(5)
    c[0].metric("Jobs tracked", sum(jc.values()))
    c[1].metric("To score (filtered)", jc.get("filtered", 0))
    c[2].metric("Sent to you (total)", ac.get("sent_to_user", 0))
    c[3].metric("Sent this week (Mon–)", this_week)
    c[4].metric("Sent last 7 days", in7)
    c = st.columns(4)
    c[0].metric(f"Scored ≥ {threshold}", jc.get("scored", 0))
    c[1].metric("Below threshold", jc.get("below_threshold", 0))
    c[2].metric("To tailor (apps scored)", ac.get("scored", 0))
    c[3].metric("Marked applied", int(sent["applied"].sum()) if not sent.empty else 0)

    left, right = st.columns(2)
    with left:
        st.caption("Jobs by status")
        st.dataframe(pd.DataFrame(sorted(jc.items(), key=lambda kv: -kv[1]), columns=["status", "jobs"]),
                     hide_index=True, width="stretch")
    with right:
        st.caption("Applications by status")
        st.dataframe(pd.DataFrame(sorted(ac.items(), key=lambda kv: -kv[1]), columns=["status", "applications"]),
                     hide_index=True, width="stretch")
    if not sent.empty:
        st.caption("Outcomes of sent matches")
        oc = sent["outcome"].value_counts().rename_axis("outcome").reset_index(name="n")
        st.dataframe(oc, hide_index=True)

    manual = q("""SELECT title, company_name AS company, location, url AS link FROM jobs
                  WHERE status = 'manual_apply' ORDER BY first_seen_at DESC LIMIT 50""")
    if not manual.empty:
        st.caption("Manual-apply leads (no public ATS posting found)")
        st.dataframe(manual, hide_index=True, column_config={"link": st.column_config.LinkColumn("link")})


def sent_view() -> None:
    st.subheader("Sent matches")
    st.caption("Listings + cover letters sent to you on Telegram, newest first. You apply yourself.")
    df = sent_apps()
    if df.empty:
        st.info("Nothing has been sent yet.")
        return
    show = df[["app_id", "sent_at", "company", "title", "score", "location", "outcome", "applied", "link"]].copy()
    show["sent_at"] = show["sent_at"].dt.strftime("%Y-%m-%d %H:%M %Z")
    st.dataframe(show, hide_index=True, width="stretch",
                 column_config={"link": st.column_config.LinkColumn("apply / listing"),
                                "score": st.column_config.NumberColumn("score", format="%d"),
                                "applied": st.column_config.CheckboxColumn("applied")})

    st.markdown("**Cover letter PDF**")
    docs = cover_letters(df.app_id.astype(int).tolist())
    if docs is None:
        st.caption("Cover letter downloads unavailable (the dashboard role can't read candidate_documents).")
        return
    if docs.empty:
        st.caption("No stored cover letter PDFs for these applications.")
        return
    labels = {int(r.app_id): f"#{int(r.app_id)} {r.company or '?'} – {r.title}" for r in df.itertuples()}
    doc_by_app = {int(r.app_id): (int(r.doc_id), r.filename) for r in docs.itertuples()}
    choice = st.selectbox("Application", [a for a in labels if a in doc_by_app], format_func=labels.get,
                          key="cl_app")
    if choice is not None:
        doc_id, fname = doc_by_app[choice]
        data = document_bytes(doc_id)
        if data:
            st.download_button("Download cover letter PDF", data=data, file_name=fname or f"cover_letter_{choice}.pdf",
                               mime="application/pdf", key=f"dl_{doc_id}")
        else:
            st.caption("Couldn't load that PDF.")


def outcome_view() -> None:
    st.subheader("Mark outcome")
    st.caption("Adds an append-only event. Outcomes are copied onto the application on the next pipeline run "
               "(same as `jobbuddy outcome <id> <value>`). 'I applied' is a note only; status stays sent_to_user.")
    df = sent_apps()
    if df.empty:
        st.info("No sent applications yet.")
        return
    labels = {int(r.app_id): f"#{int(r.app_id)} {r.company or '?'} – {r.title}  (now: {r.outcome}"
              f"{', applied' if r.applied else ''})" for r in df.itertuples()}
    with st.form("outcome_form"):
        app_id = st.selectbox("Application", list(labels), format_func=labels.get)
        outcome = st.selectbox("Outcome", OUTCOMES, index=OUTCOMES.index("interview"),
                               format_func=lambda o: OUTCOME_LABELS.get(o, o))
        b1, b2 = st.columns(2)
        do_applied = b1.form_submit_button("I applied")
        do_outcome = b2.form_submit_button("Save outcome")
    try:
        if do_applied:
            insert_applied_event(int(app_id))
            st.success(f"Recorded: you applied to {labels[int(app_id)].split('  (')[0]}.")
        elif do_outcome:
            insert_outcome_event(int(app_id), outcome)
            st.success(f"Recorded outcome '{OUTCOME_LABELS[outcome]}' for {labels[int(app_id)].split('  (')[0]}.")
    except Exception as exc:
        st.error(f"Couldn't save ({type(exc).__name__}). Nothing was changed.")


def scores_view(threshold: int) -> None:
    st.subheader("Recent scores")
    min_score = st.slider("Minimum score", 0, 100, 0, key="min_score")
    df = q("""SELECT s.created_at AS scored_at, s.llm_score AS score, j.company_name AS company, j.title,
                     j.location, COALESCE(j.url, j.apply_url) AS link, j.status AS job_status, s.llm_reasons
              FROM job_scores s JOIN jobs j ON j.id = s.job_id
              WHERE s.llm_score IS NOT NULL AND s.llm_score >= :m
                AND s.id = (SELECT MAX(s2.id) FROM job_scores s2 WHERE s2.job_id = s.job_id AND s2.llm_score IS NOT NULL)
              ORDER BY s.id DESC LIMIT 100""", m=int(min_score))
    if df.empty:
        st.info("No scored jobs match.")
        return
    df["scored_at"] = to_local(df["scored_at"]).dt.strftime("%Y-%m-%d %H:%M %Z")
    df["passed"] = df["score"] >= threshold
    df["reasons"] = df["llm_reasons"].map(reasons_text)
    st.caption(f"{len(df)} most recent (one row per job, latest score). Threshold {threshold}.")
    st.dataframe(df[["scored_at", "score", "passed", "company", "title", "location", "job_status", "link", "reasons"]],
                 hide_index=True, width="stretch",
                 column_config={"link": st.column_config.LinkColumn("link"),
                                "passed": st.column_config.CheckboxColumn(f"≥ {threshold}"),
                                "reasons": st.column_config.TextColumn("reasons", width="large")})


def main() -> None:
    st.set_page_config(page_title="jobbuddy", layout="wide")
    st.title("jobbuddy")
    if not gate():
        return
    threshold = score_threshold()
    st.caption(f"Score threshold: {threshold} · Sent matches arrive on Telegram; you apply yourself.")
    try:
        engine().connect().close()
    except Exception as exc:
        st.error(f"Can't reach the database ({type(exc).__name__}). Check the DATABASE_URL secret.")
        return
    tabs = st.tabs(["Overview", "Sent matches", "Mark outcome", "Recent scores"])
    with tabs[0]:
        overview_view(threshold)
    with tabs[1]:
        sent_view()
    with tabs[2]:
        outcome_view()
    with tabs[3]:
        scores_view(threshold)


if __name__ == "__main__":
    main()
