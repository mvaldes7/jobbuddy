"""0002: allow applications.status = 'sent_to_user' (jobbuddy sends Mike the listing + cover letter; no auto-apply).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

_OLD = ["found", "filtered", "scored", "tailored", "filled", "awaiting_approval", "approved", "submitted",
        "failed", "skipped", "paused", "needs_answers"]
_NEW = _OLD + ["sent_to_user"]


def _check(values):
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("applications") as b:
        b.drop_constraint("ck_applications_status", type_="check")
        b.create_check_constraint("ck_applications_status", _check(_NEW))


def downgrade() -> None:
    op.execute("UPDATE applications SET status = 'skipped' WHERE status = 'sent_to_user'")
    with op.batch_alter_table("applications") as b:
        b.drop_constraint("ck_applications_status", type_="check")
        b.create_check_constraint("ck_applications_status", _check(_OLD))
