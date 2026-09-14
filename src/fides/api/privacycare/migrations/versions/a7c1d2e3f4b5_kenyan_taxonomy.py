"""kenyan taxonomy

Revision ID: a7c1d2e3f4b5
Revises: dca42e735806
Create Date: 2026-09-14 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'a7c1d2e3f4b5'
down_revision = 'dca42e735806'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('privacycare_taxonomy_mapping',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('taxonomy', sa.String(length=32), nullable=False),
    sa.Column('customer_term', sa.String(length=255), nullable=False),
    sa.Column('fides_key', sa.String(length=255), nullable=True),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('source_document', sa.String(length=255), nullable=False),
    sa.Column('names_person_directly', sa.Boolean(), nullable=True),
    sa.Column('natural_person_role', sa.String(length=255), nullable=True),
    sa.Column('appears_via', sa.String(length=255), nullable=True),
    sa.Column('deferred_to', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('taxonomy', 'customer_term', name='uq_privacycare_taxonomy_mapping_term')
    )
    op.create_table('privacycare_processing_ground',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('ground', sa.String(length=255), nullable=False),
    sa.Column('source_class', sa.String(length=64), nullable=True),
    sa.Column('fides_legal_basis', sa.String(length=64), nullable=True),
    sa.Column('suggested_fides_legal_basis', sa.String(length=64), nullable=True),
    sa.Column('provenance', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ground')
    )
    op.create_table('privacycare_declaration_ground',
    sa.Column('id', sa.String(length=255), nullable=False),
    sa.Column('privacy_declaration_id', sa.String(length=255), nullable=False),
    sa.Column('processing_ground_id', sa.String(length=255), nullable=False),
    sa.Column('recorded_by', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['processing_ground_id'], ['privacycare_processing_ground.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('privacy_declaration_id')
    )
    op.create_index(op.f('ix_privacycare_declaration_ground_processing_ground_id'), 'privacycare_declaration_ground', ['processing_ground_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_privacycare_declaration_ground_processing_ground_id'), table_name='privacycare_declaration_ground')
    op.drop_table('privacycare_declaration_ground')
    op.drop_table('privacycare_processing_ground')
    op.drop_table('privacycare_taxonomy_mapping')
