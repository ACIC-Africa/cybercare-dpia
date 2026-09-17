"""generation_skip

Revision ID: 443430b60890
Revises: 54c8fce54023
Create Date: 2026-09-17 00:00:00.000000

Creates privacycare_generation_skip — one new PrivacyCare-owned table in
PrivacyCare's own Alembic chain. Alters no Ethyca table or type: in
particular, this migration does NOT touch privacy_assessment_task, whose
only columns for a run's outcome are total_count, completed_count and a
free-text message. That table is Ethyca's, and adding a column to it is
the merge-hostile move this fork has avoided throughout (see
db/migration's retirement and every prior privacycare_* migration's own
docstring for the same rule).

Plan 19, Task 1 (screening visibility): the screening gate landed in plan
18 already decides which processing activities get skipped before a DPIA
is generated, but the count of how many is skipped goes only into that
free-text message, which nothing else reads. This table gives it a place
to live that a query can read back — one row per generation run that
skipped anything, keyed by task_id (privacy_assessment_task.id, an ETHYCA
id, carried with no ForeignKey for the same reason
privacycare_dpia_risk.assessment_id and
privacycare_screening_decision.declaration_id carry none: a constraint
from our chain into theirs is the coupling that breaks an upstream merge).

The UNIQUE constraint on task_id is deliberate and different from
privacycare_screening_decision's append-only shape: a task id names one
generation run, not an evolving fact re-assessed over time, so there is
exactly one row to have per run, upserted rather than appended.

No row is written for a run that skipped nothing — an absent row reads as
zero (see tasks.py's skipped_for_task), so a run with nothing to say about
skipping leaves nothing behind rather than a stored zero.
"""
import sqlalchemy as sa
from alembic import op

revision = '443430b60890'
down_revision = '54c8fce54023'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'privacycare_generation_skip',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('task_id', sa.String(length=255), nullable=False),
        sa.Column('skipped_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', name='uq_privacycare_generation_skip_task_id'),
    )


def downgrade():
    op.drop_table('privacycare_generation_skip')
