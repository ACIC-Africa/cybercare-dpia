"""dsr register

Revision ID: ff91bc4d23d9
Revises: a7c1d2e3f4b5
Create Date: 2026-09-15 09:00:00.000000

Creates privacycare_dsr_timeline and privacycare_dsr_request — both new
PrivacyCare-owned tables in PrivacyCare's own Alembic chain. Touches no
Ethyca table: no ALTER, no FK, and no reference lands on ctl_systems,
privacydeclaration, privacyrequest, or policy. privacycare_dsr_request.
fides_privacy_request_id carries NO FOREIGN KEY (it IS indexed, by
ix_privacycare_dsr_request_fides_privacy_request_id below, for lookup
speed) — same reason process_declaration_table.privacy_declaration_id
carries no FK: that row's lifecycle belongs to upstream Fides, and a
migration in PrivacyCare's own chain may not constrain against a table it
does not own.
"""
import sqlalchemy as sa
from alembic import op

revision = 'ff91bc4d23d9'
down_revision = 'a7c1d2e3f4b5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('privacycare_dsr_timeline',
    sa.Column('right', sa.String(length=32), nullable=False),
    sa.Column('days', sa.Integer(), nullable=True),
    sa.Column('source_note', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('right')
    )
    op.create_table('privacycare_dsr_request',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('right', sa.String(length=32), nullable=False),
    sa.Column('subject_identifier', sa.String(length=255), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('owner_email', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='open', nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=True),
    sa.Column('outcome_grounds', sa.Text(), nullable=True),
    sa.Column('decided_by', sa.String(length=255), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('subject_notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fides_privacy_request_id', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['right'], ['privacycare_dsr_timeline.right'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_privacycare_dsr_request_fides_privacy_request_id'), 'privacycare_dsr_request', ['fides_privacy_request_id'], unique=False)
    op.create_index(op.f('ix_privacycare_dsr_request_right'), 'privacycare_dsr_request', ['right'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_privacycare_dsr_request_right'), table_name='privacycare_dsr_request')
    op.drop_index(op.f('ix_privacycare_dsr_request_fides_privacy_request_id'), table_name='privacycare_dsr_request')
    op.drop_table('privacycare_dsr_request')
    op.drop_table('privacycare_dsr_timeline')
