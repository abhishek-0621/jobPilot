"""control flags (pause survives restart)

Revision ID: 0002_control
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa


revision = '0002_control'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('control_flag',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    op.drop_table('control_flag')
