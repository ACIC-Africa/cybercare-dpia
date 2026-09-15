"""dsr business process id

Revision ID: c9a1e5b7d3f2
Revises: 05b1920196e4
Create Date: 2026-09-15 12:00:00.000000

Final review wave on plan 14 task 4 (minor finding): record_request already
takes a `business_process_id` argument and reads it to resolve
owner_source="business_process", but the row never recorded WHICH process
that was — the register could say a request's owner came from a business
process without being able to name it, half of an auditable claim. This
column gives it somewhere to live, same nullable-for-backfill reasoning as
05b1920196e4_dsr_owner_source.py: privacycare_dsr_request holds zero live
rows as of this migration (nothing to backfill), but a hand-written INSERT
or a future write path that never passes business_process_id must remain a
legal row.

FK to privacycare_business_process.id, ondelete='SET NULL' — deliberately
not CASCADE: a business process being deleted must not delete or block
deletion of a regulatory record that named it; the record survives with
the link cleared.
"""
import sqlalchemy as sa
from alembic import op

revision = 'c9a1e5b7d3f2'
down_revision = '05b1920196e4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'privacycare_dsr_request',
        sa.Column('business_process_id', sa.String(length=255), nullable=True),
    )
    op.create_index(
        op.f('ix_privacycare_dsr_request_business_process_id'),
        'privacycare_dsr_request',
        ['business_process_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_privacycare_dsr_request_business_process_id',
        'privacycare_dsr_request',
        'privacycare_business_process',
        ['business_process_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_privacycare_dsr_request_business_process_id',
        'privacycare_dsr_request',
        type_='foreignkey',
    )
    op.drop_index(
        op.f('ix_privacycare_dsr_request_business_process_id'),
        table_name='privacycare_dsr_request',
    )
    op.drop_column('privacycare_dsr_request', 'business_process_id')
