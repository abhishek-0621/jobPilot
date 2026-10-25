"""application.submit_live_intent

Revision ID: 0008_submit_live_intent
Revises: 0007_submit_requested
"""
import sqlalchemy as sa
from alembic import op


revision = '0008_submit_live_intent'
down_revision = '0007_submit_requested'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('application') as batch:
        batch.add_column(sa.Column('submit_live_intent', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('application') as batch:
        batch.drop_column('submit_live_intent')
