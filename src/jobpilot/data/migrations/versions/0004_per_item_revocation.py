"""per-item approval revocation

Revision ID: 0004_per_item_revocation
Revises: 0003_audit_replace_guard
"""
import sqlalchemy as sa
from alembic import op


revision = '0004_per_item_revocation'
down_revision = '0003_audit_replace_guard'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('approval') as batch:
        batch.add_column(sa.Column('revoked_item_ids_json', sa.Text(), nullable=False,
                                   server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('approval') as batch:
        batch.drop_column('revoked_item_ids_json')
