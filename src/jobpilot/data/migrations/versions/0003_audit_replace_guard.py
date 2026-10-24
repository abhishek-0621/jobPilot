"""block INSERT OR REPLACE over existing audit events

Revision ID: 0003_audit_replace_guard
Revises: 0002_control
"""
from alembic import op


revision = '0003_audit_replace_guard'
down_revision = '0002_control'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER audit_event_no_replace BEFORE INSERT ON audit_event "
        "WHEN EXISTS (SELECT 1 FROM audit_event WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT,'audit_event is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_event_no_replace")
