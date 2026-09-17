"""screening

Revision ID: 54c8fce54023
Revises: 51f13cfe1a1b
Create Date: 2026-09-17 00:00:00.000000

Creates privacycare_screening_trigger and privacycare_screening_decision —
two new PrivacyCare-owned tables in PrivacyCare's own Alembic chain. Alters
no Ethyca table or type: in particular, this migration does NOT touch
Ethyca's assessmentstatus enum (in_progress, completed, outdated,
generating) — there is no screened_out value there, and there will not
be. The screening verdict lives on privacycare_screening_decision, not on
privacy_assessment, the same separation plan 17's 51f13cfe1a1b drew
between our risk register and Ethyca's risklevel enum.

privacycare_screening_trigger holds the six questions a screener answers
before a DPIA exists. It is a table rather than a constant because the
wording belongs to the privacy SME (Carol) and she will revise it — two of
the six were already recast once, on 2026-09-17, from a university sample
case to this customer's world. Same shape and same reason as
privacycare_dsr_timeline (bf6d64cab75c) and privacycare_consent_rule. This
migration does NOT insert the six rows — populating the table is the seed
CLI's job (scripts/privacycare/seed_screening_triggers.py), not this
migration's, the same separation seed_dsr.py already draws between
creating a table and seeding its runtime prerequisites.

privacycare_screening_decision.declaration_id references
privacydeclaration.id, an ETHYCA table, but carries no ForeignKey — a
constraint from our chain into theirs is the coupling that breaks an
upstream merge (same rule as privacycare_dpia_risk.assessment_id and
privacycare_dsr_request.fides_privacy_request_id). Existence is checked at
write time, not by the database.

The CheckConstraint on privacycare_screening_decision
(ck_screening_screenout_has_a_reason) makes a screen-out without a
justification impossible even from hand-written SQL: justification must be
NULL when dpia_required is true, and a non-empty (after trim) string when
it is false. This is deliberate — the justification is the compliance
artifact a regulator asks for when they ask why an activity has no
assessment, and an artifact that can be empty is not one.

privacycare_screening_decision is APPEND-ONLY by convention (enforced by
the application, not by the schema): re-screening writes a new row rather
than updating an old one, so an earlier decision — and the basis for it —
survives a later re-screening of the same declaration. There is
deliberately no UNIQUE constraint on declaration_id.
"""
import sqlalchemy as sa
from alembic import op

revision = '54c8fce54023'
down_revision = '51f13cfe1a1b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'privacycare_screening_trigger',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('trigger_key', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('trigger_key', name='uq_privacycare_screening_trigger_trigger_key'),
    )

    op.create_table(
        'privacycare_screening_decision',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('declaration_id', sa.String(length=255), nullable=False),
        sa.Column('dpia_required', sa.Boolean(), nullable=False),
        sa.Column('triggered_keys', sa.ARRAY(sa.String()), nullable=False),
        sa.Column('justification', sa.Text(), nullable=True),
        sa.Column('decided_by', sa.String(length=255), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "(dpia_required AND justification IS NULL) OR "
            "(NOT dpia_required AND justification IS NOT NULL "
            " AND length(trim(justification)) > 0)",
            name='ck_screening_screenout_has_a_reason',
        ),
    )
    op.create_index(
        op.f('ix_privacycare_screening_decision_declaration_id'),
        'privacycare_screening_decision',
        ['declaration_id'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_privacycare_screening_decision_declaration_id'),
        table_name='privacycare_screening_decision',
    )
    op.drop_table('privacycare_screening_decision')
    op.drop_table('privacycare_screening_trigger')
