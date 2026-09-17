"""screening decision business process

Revision ID: e9884e6bfc89
Revises: 443430b60890
Create Date: 2026-09-17 00:00:00.000000

Plan 20, Task 1 (screening re-key). The screening gate (plan 18) shipped
keyed to privacydeclaration — this platform's processing activity — but
there are exactly 2 of those in the whole system, and both hang off a
business process this codebase invented. The privacy SME confirmed on
2026-09-17 that screening belongs on the customer's own business
processes (86 of them, from her register), not on a synthetic processing
activity. This migration re-keys the one column that made that mistake:
privacycare_screening_decision.declaration_id becomes
privacycare_screening_decision.business_process_id, referencing
privacycare_business_process.id.

Renamed in place, not backfilled: privacycare_screening_decision holds
ZERO rows as of this migration (the screening gate shipped last week and
nothing has written through it yet), so there is no data to map from one
key to the other — see this task's own report for the row count actually
checked before writing this migration.

The column now carries a real ForeignKey, which it deliberately did not
before. The old "no ForeignKey" comment (models.py's
screening_decision_table) was specifically about a constraint reaching
from PrivacyCare's own Alembic chain into an ETHYCA table — that coupling
is the thing that breaks an upstream merge, and it is why
privacycare_dpia_risk.assessment_id, privacycare_generation_skip.task_id
and privacycare_process_declaration.privacy_declaration_id all still
carry none. That reasoning does not apply here: privacycare_business_process
is a PrivacyCare table, created and migrated in this same chain
(e4ea861eb93c), so a real ForeignKey is safe and is added.

ondelete is deliberately left UNSET (Postgres' default, RESTRICT) rather
than CASCADE or SET NULL: a business process must not be deletable out
from under the screening decisions recorded against it. This differs on
purpose from c9a1e5b7d3f2's privacycare_dsr_request.business_process_id
(SET NULL) — a DSR request is a regulatory record that must survive its
process link being cleared, whereas a screening decision's business
process is the whole reason the decision exists; there is no meaningful
"orphaned screening decision."

Alters exactly one PrivacyCare table (privacycare_screening_decision).
Touches no Ethyca table or type.
"""
import sqlalchemy as sa
from alembic import op

revision = 'e9884e6bfc89'
down_revision = '443430b60890'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index(
        op.f('ix_privacycare_screening_decision_declaration_id'),
        table_name='privacycare_screening_decision',
    )
    op.alter_column(
        'privacycare_screening_decision',
        'declaration_id',
        new_column_name='business_process_id',
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_index(
        op.f('ix_privacycare_screening_decision_business_process_id'),
        'privacycare_screening_decision',
        ['business_process_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_privacycare_screening_decision_business_process_id',
        'privacycare_screening_decision',
        'privacycare_business_process',
        ['business_process_id'],
        ['id'],
    )


def downgrade():
    op.drop_constraint(
        'fk_privacycare_screening_decision_business_process_id',
        'privacycare_screening_decision',
        type_='foreignkey',
    )
    op.drop_index(
        op.f('ix_privacycare_screening_decision_business_process_id'),
        table_name='privacycare_screening_decision',
    )
    op.alter_column(
        'privacycare_screening_decision',
        'business_process_id',
        new_column_name='declaration_id',
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_index(
        op.f('ix_privacycare_screening_decision_declaration_id'),
        'privacycare_screening_decision',
        ['declaration_id'],
        unique=False,
    )
