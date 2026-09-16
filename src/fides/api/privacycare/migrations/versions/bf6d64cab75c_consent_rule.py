"""consent rule

Revision ID: bf6d64cab75c
Revises: 3a48cbf9edc3
Create Date: 2026-09-16 00:48:46.419165

Creates privacycare_consent_rule — a new PrivacyCare-owned table in
PrivacyCare's own Alembic chain. Touches no Ethyca table: it carries no
foreign key at all, Fides or otherwise.

Plan 16 task 1: what counts as a notice change material enough to
invalidate consent already given (D-CON-2) is still an open question for
the SME (OQ-CON-01). A single configuration row, rather than a constant in
code, means Carol's ruling lands without a deploy — the same shape and for
the same reason privacycare_dsr_timeline exists as a table rather than a
tuple in dsr/timelines.py.

This migration does NOT insert a default row. Populating the row is
Task 4's seed CLI's job, not this migration's — the same separation
seed_dsr.py already draws between creating a table (a migration's job)
and seeding its runtime prerequisites (a script's job, run after Fides
itself is up). An empty table is deliberate here: `active_rule` (see
consent/materiality.py) raises rather than reading an empty table as a
meaningful default.
"""
import sqlalchemy as sa
from alembic import op

revision = 'bf6d64cab75c'
down_revision = '3a48cbf9edc3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('privacycare_consent_rule',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('rule', sa.String(length=64), nullable=False),
    sa.Column('source_note', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('privacycare_consent_rule')
