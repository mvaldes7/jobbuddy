"""0001 initial schema: vector extension + full jobbuddy schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-16

Run ONLY with DATABASE_URL_DIRECT (owner role). Postgres extras created here:
  * CREATE EXTENSION IF NOT EXISTS vector
  * HNSW index on jobs.embedding (vector_cosine_ops)
  * append-only triggers on application_events (UPDATE/DELETE raise)
  * least-privilege grants for jobbuddy_app (pipeline) and jobbuddy_dashboard, if those roles exist
SQLite gets equivalent append-only triggers; Azure SQL uses sql/azure_schema.sql.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from jobbuddy.db.types import Embedding

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


PG_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION jobbuddy_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'application_events is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_application_events_append_only ON application_events;
CREATE TRIGGER trg_application_events_append_only
  BEFORE UPDATE OR DELETE ON application_events
  FOR EACH ROW EXECUTE FUNCTION jobbuddy_append_only();
"""

# Least privilege. Roles are created by the DB owner outside of migrations (no passwords in code).
# Create them with SQL as the owner, not the Neon console/API: console/API roles join neon_superuser.
# jobbuddy_dashboard is read-only except INSERT on the append-only application_events.
PG_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jobbuddy_app') THEN
    GRANT USAGE ON SCHEMA public TO jobbuddy_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO jobbuddy_app;
    REVOKE UPDATE, DELETE ON application_events FROM jobbuddy_app;
    REVOKE DELETE ON approvals FROM jobbuddy_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO jobbuddy_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jobbuddy_dashboard') THEN
    GRANT USAGE ON SCHEMA public TO jobbuddy_dashboard;
    GRANT SELECT ON companies, jobs, job_scores, applications, application_events, approvals,
                    screenshots, field_questions, llm_usage, pipeline_runs, candidate_documents
                    TO jobbuddy_dashboard;
    GRANT INSERT ON application_events TO jobbuddy_dashboard;
    GRANT USAGE, SELECT ON SEQUENCE application_events_id_seq TO jobbuddy_dashboard;
  END IF;
END
$$;
"""

SQLITE_APPEND_ONLY = [
    "CREATE TRIGGER IF NOT EXISTS trg_application_events_no_update BEFORE UPDATE ON application_events "
    "BEGIN SELECT RAISE(ABORT, 'application_events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_application_events_no_delete BEFORE DELETE ON application_events "
    "BEGIN SELECT RAISE(ABORT, 'application_events is append-only'); END",
]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('companies',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('ats', sa.String(length=32), nullable=False),
    sa.Column('board_token', sa.String(length=255), nullable=False),
    sa.Column('careers_url', sa.String(length=1024), nullable=True),
    sa.Column('ats_config', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ats', 'board_token', name='uq_companies_ats_board')
    )
    op.create_table('form_field_mappings',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('ats', sa.String(length=32), nullable=False),
    sa.Column('label_normalized', sa.String(length=512), nullable=False),
    sa.Column('field_type', sa.String(length=32), nullable=True),
    sa.Column('profile_key', sa.String(length=255), nullable=True),
    sa.Column('static_value', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('times_used', sa.Integer(), nullable=False),
    sa.Column('times_confirmed', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ats', 'label_normalized', name='uq_form_field_mappings_ats_label')
    )
    op.create_table('kv_state',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('llm_usage',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=True),
    sa.Column('purpose', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=255), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_llm_usage_run', 'llm_usage', ['run_id'], unique=False)

    op.create_table('pipeline_runs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trigger', sa.String(length=32), nullable=True),
    sa.Column('stats', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('processed_emails',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('message_id_sha256', sa.String(length=64), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=True),
    sa.Column('leads_found', sa.Integer(), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id_sha256', name='uq_processed_emails_msgid')
    )
    op.create_table('jobs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('company_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('external_job_id', sa.String(length=255), nullable=False),
    sa.Column('company_name', sa.String(length=255), nullable=True),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('location', sa.String(length=512), nullable=True),
    sa.Column('remote', sa.Boolean(), nullable=True),
    sa.Column('url', sa.String(length=2048), nullable=True),
    sa.Column('apply_url', sa.String(length=2048), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('salary_min', sa.Integer(), nullable=True),
    sa.Column('salary_max', sa.Integer(), nullable=True),
    sa.Column('salary_currency', sa.String(length=8), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('lead_meta', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('embedding', Embedding(384), nullable=True),
    sa.CheckConstraint("status IN ('found', 'filtered', 'filtered_out', 'scored', 'below_threshold', 'closed', 'manual_apply')", name='ck_jobs_status'),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'external_job_id', name='uq_jobs_source_external')
    )
    op.create_index('ix_jobs_company', 'jobs', ['company_id'], unique=False)
    op.create_index('ix_jobs_status', 'jobs', ['status'], unique=False)

    op.create_table('applications',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('ats', sa.String(length=32), nullable=True),
    sa.Column('tailored_resume', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('cover_letter_text', sa.Text(), nullable=True),
    sa.Column('answers', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('filled_state', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('low_confidence', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('human_edits', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('pause_reason', sa.String(length=255), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_via', sa.String(length=32), nullable=True),
    sa.Column('approval_nonce', sa.String(length=64), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("outcome IN ('none', 'interview', 'rejected', 'offer', 'ghosted', 'withdrawn')", name='ck_applications_outcome'),
    sa.CheckConstraint("status <> 'submitted' OR approved_at IS NOT NULL", name='ck_applications_submit_requires_approval'),
    sa.CheckConstraint("status IN ('found', 'filtered', 'scored', 'tailored', 'filled', 'awaiting_approval', 'approved', 'submitted', 'failed', 'skipped', 'paused', 'needs_answers')", name='ck_applications_status'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id', name='uq_applications_job')
    )
    op.create_index('ix_applications_status', 'applications', ['status'], unique=False)

    op.create_table('job_scores',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('filter_passed', sa.Boolean(), nullable=False),
    sa.Column('filter_reasons', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('similarity', sa.Float(), nullable=True),
    sa.Column('embedding_model', sa.String(length=255), nullable=True),
    sa.Column('llm_score', sa.Integer(), nullable=True),
    sa.Column('llm_reasons', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('llm_model', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_scores_job', 'job_scores', ['job_id'], unique=False)

    op.create_table('application_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('application_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('from_status', sa.String(length=32), nullable=True),
    sa.Column('to_status', sa.String(length=32), nullable=True),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_application_events_app', 'application_events', ['application_id'], unique=False)

    op.create_table('approvals',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('application_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('nonce', sa.String(length=64), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('decided_via', sa.String(length=16), nullable=False),
    sa.Column('telegram_user_id', sa.String(length=32), nullable=True),
    sa.Column('edits', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("decided_via IN ('telegram', 'dashboard', 'cli', 'slack', 'email')", name='ck_approvals_via'),
    sa.CheckConstraint("decision IN ('approve', 'edit', 'skip')", name='ck_approvals_decision'),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('nonce', name='uq_approvals_nonce')
    )
    op.create_index('ix_approvals_app', 'approvals', ['application_id'], unique=False)

    op.create_table('candidate_documents',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=128), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('resume', 'cover_letter_sample', 'tailored_resume', 'cover_letter')", name='ck_candidate_documents_kind'),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_candidate_documents_kind', 'candidate_documents', ['kind'], unique=False)

    op.create_table('screenshots',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('application_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('tag', sa.String(length=32), nullable=False),
    sa.Column('png', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_screenshots_app', 'screenshots', ['application_id'], unique=False)

    op.create_table('field_questions',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('application_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('ats', sa.String(length=32), nullable=True),
    sa.Column('field_id', sa.String(length=255), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('label_normalized', sa.String(length=512), nullable=True),
    sa.Column('field_type', sa.String(length=32), nullable=True),
    sa.Column('required', sa.Boolean(), nullable=False),
    sa.Column('options', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('max_length', sa.Integer(), nullable=True),
    sa.Column('page_url', sa.String(length=2048), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('answer', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('answered_by', sa.String(length=32), nullable=True),
    sa.Column('reusable', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'answered', 'dismissed')", name='ck_field_questions_status'),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('application_id', 'field_id', name='uq_field_questions_app_field')
    )
    op.create_index('ix_field_questions_status', 'field_questions', ['status'], unique=False)


    if dialect == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_embedding_hnsw ON jobs "
                   "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)")
        op.execute(PG_APPEND_ONLY)
        op.execute(PG_GRANTS)
    elif dialect == "sqlite":
        for stmt in SQLITE_APPEND_ONLY:
            op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_application_events_append_only ON application_events")
        op.execute("DROP FUNCTION IF EXISTS jobbuddy_append_only()")
        op.execute("DROP INDEX IF EXISTS ix_jobs_embedding_hnsw")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_application_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_application_events_no_delete")
    op.drop_index('ix_field_questions_status', table_name='field_questions')
    op.drop_table('field_questions')
    op.drop_index('ix_screenshots_app', table_name='screenshots')

    op.drop_table('screenshots')
    op.drop_index('ix_candidate_documents_kind', table_name='candidate_documents')

    op.drop_table('candidate_documents')
    op.drop_index('ix_approvals_app', table_name='approvals')

    op.drop_table('approvals')
    op.drop_index('ix_application_events_app', table_name='application_events')

    op.drop_table('application_events')
    op.drop_index('ix_job_scores_job', table_name='job_scores')

    op.drop_table('job_scores')
    op.drop_index('ix_applications_status', table_name='applications')

    op.drop_table('applications')
    op.drop_index('ix_jobs_status', table_name='jobs')
    op.drop_index('ix_jobs_company', table_name='jobs')

    op.drop_table('jobs')
    op.drop_table('processed_emails')
    op.drop_table('pipeline_runs')
    op.drop_index('ix_llm_usage_run', table_name='llm_usage')

    op.drop_table('llm_usage')
    op.drop_table('kv_state')
    op.drop_table('form_field_mappings')
    op.drop_table('companies')
