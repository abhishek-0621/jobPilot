"""application.submit_requested_at

Revision ID: 0007_submit_requested
Revises: 0006_form_preparation
"""
import sqlalchemy as sa
from alembic import op


revision = '0007_submit_requested'
down_revision = '0006_form_preparation'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('application') as batch:
        batch.add_column(sa.Column('submit_requested_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('application') as batch:
        batch.drop_column('submit_requested_at')
