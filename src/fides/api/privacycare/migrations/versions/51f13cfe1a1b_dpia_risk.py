"""dpia risk

Revision ID: 51f13cfe1a1b
Revises: bf6d64cab75c
Create Date: 2026-09-16 00:00:00.000000

Creates privacycare_dpia_risk — one new PrivacyCare-owned table in
PrivacyCare's own Alembic chain. Alters no Ethyca table or type: in
particular, this migration does NOT touch Ethyca's risklevel enum
(privacy_assessment.risk_level) — see risk/banding.py's projected_risk_level
for how our four-value band maps down to that three-value enum without
widening it.

assessment_id references privacy_assessment.id, an Ethyca table, but
carries no ForeignKey — a constraint from our chain into theirs is the
coupling that breaks an upstream merge (same rule as
privacycare_dsr_request.fides_privacy_request_id and
privacycare_process_declaration.privacy_declaration_id). Existence is
checked at write time by register.add_risk, not by the database.

score and band are deliberately absent as columns. Plan 17 task 2: both are
computed from likelihood and severity on read (risk/register.py, through
risk/banding.py's score()/band()), so a future change to the banding rule
cannot leave a stale number sitting in a compliance record someone later
has to defend.
"""
import sqlalchemy as sa
from alembic import op

revision = '51f13cfe1a1b'
down_revision = 'bf6d64cab75c'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'privacycare_dpia_risk',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('assessment_id', sa.String(length=255), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('likelihood', sa.Integer(), nullable=False),
        sa.Column('severity', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint('likelihood BETWEEN 1 AND 5', name='ck_dpia_risk_likelihood'),
        sa.CheckConstraint('severity BETWEEN 1 AND 5', name='ck_dpia_risk_severity'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_privacycare_dpia_risk_assessment_id'),
        'privacycare_dpia_risk',
        ['assessment_id'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_privacycare_dpia_risk_assessment_id'),
        table_name='privacycare_dpia_risk',
    )
    op.drop_table('privacycare_dpia_risk')
