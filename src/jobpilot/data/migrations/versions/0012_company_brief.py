"""company_brief table (company research for why-company answers)

Revision ID: 0012_company_brief
Revises: 0011_one_active_discover
"""
import sqlalchemy as sa
from alembic import op


revision = '0012_company_brief'
down_revision = '0011_one_active_discover'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'company_brief',
        sa.Column('company_key', sa.String(), nullable=False),
        sa.Column('company', sa.String(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sources_json', sa.Text(), nullable=False),
        sa.Column('items_json', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('company_key'),
    )


def downgrade() -> None:
    op.drop_table('company_brief')
