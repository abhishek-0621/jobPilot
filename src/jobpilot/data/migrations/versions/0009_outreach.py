"""contact_candidate address/kind/application_id/created_at; outreach_draft created_at,
skip_reason, sent_note

Revision ID: 0009_outreach
Revises: 0008_submit_live_intent
"""
import sqlalchemy as sa
from alembic import op


revision = '0009_outreach'
down_revision = '0008_submit_live_intent'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('contact_candidate') as batch:
        batch.add_column(sa.Column('address', sa.String(), nullable=True))
        batch.add_column(sa.Column('kind', sa.String(), nullable=True))
        batch.add_column(sa.Column('application_id', sa.String(), nullable=True))
        batch.add_column(sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                                   server_default=sa.func.current_timestamp()))
        batch.create_foreign_key('fk_contact_candidate_application', 'application',
                                 ['application_id'], ['application_id'])
    with op.batch_alter_table('outreach_draft') as batch:
        batch.add_column(sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                                   server_default=sa.func.current_timestamp()))
        batch.add_column(sa.Column('skip_reason', sa.String(), nullable=True))
        batch.add_column(sa.Column('sent_note', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('outreach_draft') as batch:
        batch.drop_column('sent_note')
        batch.drop_column('skip_reason')
        batch.drop_column('created_at')
    with op.batch_alter_table('contact_candidate') as batch:
        batch.drop_constraint('fk_contact_candidate_application', type_='foreignkey')
        batch.drop_column('created_at')
        batch.drop_column('application_id')
        batch.drop_column('kind')
        batch.drop_column('address')
