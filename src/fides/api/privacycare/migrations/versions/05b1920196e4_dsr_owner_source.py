"""dsr owner source

Revision ID: 05b1920196e4
Revises: ff91bc4d23d9
Create Date: 2026-09-15 10:08:31.959308

Fix round 1 on plan 14 task 4's HTTP surface: the review caught that
DsrRequestResponse's owner_source field was being INFERRED at read time
("explicit" for any row with a non-null owner_email, "unassigned"
otherwise) rather than stored, which let a business_process- or
configured_dpo-resolved owner be reported as "explicit" — a false claim
about how the owner was determined, on a surface that records regulatory
obligations. register.resolve_owner already computes the real four-value
source (D-DSR-8: explicit | business_process | configured_dpo |
unassigned) on every record_request call; this column gives it somewhere
to live.

Nullable, not NOT NULL: privacycare_dsr_request holds zero rows as of this
migration (nothing to backfill), but a hand-written INSERT bypassing
record_request, or a future write path that never calls resolve_owner,
must remain a legal row rather than fail a constraint it cannot satisfy.
"""
import sqlalchemy as sa
from alembic import op

revision = '05b1920196e4'
down_revision = 'ff91bc4d23d9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'privacycare_dsr_request',
        sa.Column('owner_source', sa.String(length=32), nullable=True),
    )


def downgrade():
    op.drop_column('privacycare_dsr_request', 'owner_source')
