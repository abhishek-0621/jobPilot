"""document_artifact.profile_version

Revision ID: 0005_artifact_profile_version
Revises: 0004_per_item_revocation
"""
import sqlalchemy as sa
from alembic import op


revision = '0005_artifact_profile_version'
down_revision = '0004_per_item_revocation'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('document_artifact') as batch:
        batch.add_column(sa.Column('profile_version', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('document_artifact') as batch:
        batch.drop_column('profile_version')
