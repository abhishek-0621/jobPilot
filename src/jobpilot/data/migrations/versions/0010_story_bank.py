"""story and story_bank tables (Plan 9 story bank)

Revision ID: 0010_story_bank
Revises: 0009_outreach
"""
import sqlalchemy as sa
from alembic import op


revision = '0010_story_bank'
down_revision = '0009_outreach'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'story',
        sa.Column('story_id', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('themes_json', sa.Text(), nullable=False),
        sa.Column('body_json', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column('verified_by_user', sa.Boolean(), nullable=False),
        sa.Column('profile_version', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('story_id'),
    )
    op.create_table(
        'story_bank',
        sa.Column('bank_id', sa.String(), nullable=False),
        sa.Column('standing_rules_json', sa.Text(), nullable=False),
        sa.Column('canned_json', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('bank_id'),
    )


def downgrade() -> None:
    op.drop_table('story_bank')
    op.drop_table('story')
