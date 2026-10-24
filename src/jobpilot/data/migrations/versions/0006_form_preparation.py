"""application.form_snapshot_hash; form_field_answer.required / options_json

Revision ID: 0006_form_preparation
Revises: 0005_artifact_profile_version
"""
import sqlalchemy as sa
from alembic import op


revision = '0006_form_preparation'
down_revision = '0005_artifact_profile_version'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('application') as batch:
        batch.add_column(sa.Column('form_snapshot_hash', sa.String(), nullable=True))
    with op.batch_alter_table('form_field_answer') as batch:
        batch.add_column(sa.Column('required', sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column('options_json', sa.Text(), nullable=False,
                                   server_default='[]'))


def downgrade() -> None:
    with op.batch_alter_table('form_field_answer') as batch:
        batch.drop_column('options_json')
        batch.drop_column('required')
    with op.batch_alter_table('application') as batch:
        batch.drop_column('form_snapshot_hash')
