"""dsr alert

Revision ID: 3a48cbf9edc3
Revises: c9a1e5b7d3f2
Create Date: 2026-09-15 13:18:23.716950

Creates privacycare_dsr_alert — a new PrivacyCare-owned table in
PrivacyCare's own Alembic chain. Touches no Ethyca table: the only foreign
key is to privacycare_dsr_request, which this same chain created
(ff91bc4d23d9_dsr_register.py).

This table IS the once-only guarantee plan 15 / D-DSR-5 demands FOR THE
LEDGER ROW: the unique constraint on (dsr_request_id, kind) is what stops a
ROW repeating, not a flag on the request row — a flag would lose to a
second worker, a restart mid-run, or a retry. I2 (final review of plan 15):
that is narrower than "stops an alert repeating" — a second worker or a
mid-run restart racing the same alert can still re-deliver the underlying
MESSAGE before this table's insert lands; the constraint only ever
degrades the second insert to a no-op, never lets a second row through.
ondelete='CASCADE' on dsr_request_id (unlike business_process_id's SET
NULL on the parent table): an alert ledger row has no meaning once the
obligation it warned about is gone, so it does not survive deleting the
request the way a regulatory record survives losing a business-process
link.
"""
import sqlalchemy as sa
from alembic import op

revision = '3a48cbf9edc3'
down_revision = 'c9a1e5b7d3f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('privacycare_dsr_alert',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('dsr_request_id', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('recipient', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['dsr_request_id'], ['privacycare_dsr_request.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dsr_request_id', 'kind', name='uq_privacycare_dsr_alert')
    )
    op.create_index(op.f('ix_privacycare_dsr_alert_dsr_request_id'), 'privacycare_dsr_alert', ['dsr_request_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_privacycare_dsr_alert_dsr_request_id'), table_name='privacycare_dsr_alert')
    op.drop_table('privacycare_dsr_alert')
