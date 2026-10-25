"""at most one QUEUED/RUNNING DISCOVER task (Plan 10)

Revision ID: 0011_one_active_discover
Revises: 0010_story_bank
"""
import sqlalchemy as sa
from alembic import op


revision = '0011_one_active_discover'
down_revision = '0010_story_bank'
branch_labels = None
depends_on = None

_WHERE = "kind = 'DISCOVER' AND status IN ('QUEUED', 'RUNNING')"


def upgrade() -> None:
    # Pre-existing duplicates (the old check-then-insert race): keep a RUNNING one if any,
    # else the oldest QUEUED one, and cancel the other QUEUED duplicates.
    op.execute(
        "UPDATE task SET status = 'CANCELLED' WHERE kind = 'DISCOVER' AND status = 'QUEUED' "
        "AND task_id != (SELECT task_id FROM task WHERE " + _WHERE + " "
        "ORDER BY status = 'RUNNING' DESC, task_id LIMIT 1)")
    op.create_index('uq_task_active_discover', 'task', ['kind'], unique=True,
                    sqlite_where=sa.text(_WHERE))


def downgrade() -> None:
    op.drop_index('uq_task_active_discover', table_name='task')
