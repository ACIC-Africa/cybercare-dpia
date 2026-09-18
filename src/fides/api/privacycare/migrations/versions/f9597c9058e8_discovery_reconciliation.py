"""discovery reconciliation

Revision ID: f9597c9058e8
Revises: e9884e6bfc89
Create Date: 2026-09-18 00:00:00.000000

Creates privacycare_discovery_reconciliation — the missing write half of
PrivacyCare's discovery feature (Screen 4's Task from the 2026-09-18
discovery-findings-API brief).

A real discovery scan already writes real `stagedresource` rows
(1 Database, 1 Schema, 176 Tables, 1703 Fields against our own `fides-db`
as of this migration), but nothing before it lets a person say what a
discovered table IS: does it belong to a system already in the data map,
or has somebody looked and decided it holds no personal data? This table
is that decision, recorded.

WHY A NEW PRIVACYCARE TABLE RATHER THAN WRITING `stagedresource` ITSELF.
`stagedresource` already carries columns for exactly this
(`user_assigned_system_id`, `user_assigned_description`,
`user_assigned_data_categories`) — Ethyca's own Plus product uses them —
but `stagedresource` is an ETHYCA table, rebuilt from scratch by every
scan's `reconcile()` (discovery/reconcile.py): a re-scan that finds the
same table again touches its existing row not at all (D-EX-6), but a row
that is genuinely NEW under a re-created monitor, or one that momentarily
goes missing and resurrects (reconcile.py's own Resurrection paragraph),
is exactly the kind of churn a compliance record must not be exposed to.
Keying our own record to the table's stable `urn` — never to
`stagedresource.id` — in our own table, in our own Alembic chain, means a
re-scan can never silently orphan or overwrite a privacy officer's
decision, and Ethyca's own row stays exactly what every other route in
this codebase already treats it as: disposable scan output. Same pattern
`privacycare_screening_decision` already uses against
`privacydeclaration`/`privacycare_business_process` (an Ethyca-adjacent
row, not written to directly).

No ForeignKey on `stagedresource_urn` or `system_id`: both reference
Ethyca tables (`stagedresource`, `ctl_systems`), and a constraint from our
chain into theirs is the coupling that breaks an upstream merge — the same
reasoning already given for `privacycare_dpia_risk.assessment_id` and
`privacycare_generation_skip.task_id`. Existence of both is checked at
write time (discovery/findings.py), not by the database.

The CheckConstraint pair mirrors `privacycare_screening_decision`'s
`ck_screening_screenout_has_a_reason` exactly, for the same reason: an
"ignored" finding without a written reason is how a register quietly
loses something, and that must be impossible even from hand-written SQL,
not merely discouraged at the API layer. `state` is restricted to
'mapped' / 'ignored' by its own constraint — 'needs review' is never a
stored value, it is the absence of any row for a urn (see
discovery/findings.py's list query).

`privacycare_discovery_reconciliation` is APPEND-ONLY by convention
(enforced by the application, not the schema, same as
`privacycare_screening_decision`): re-reconciling a table writes a new row
rather than updating an old one, so an earlier decision — and the basis
for it — survives being revisited later.

Touches no Ethyca table or type. `privacycare_*` tables are excluded from
Fides' own autogenerate by prefix (fides_exclusion_guard.py) — this table's
name already matches that prefix, so no separate registration is needed.
"""
import sqlalchemy as sa
from alembic import op

revision = 'f9597c9058e8'
down_revision = 'e9884e6bfc89'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'privacycare_discovery_reconciliation',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('stagedresource_urn', sa.String(length=1024), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('system_id', sa.String(length=255), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('decided_by', sa.String(length=255), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "state IN ('mapped', 'ignored')",
            name='ck_discovery_reconciliation_state',
        ),
        sa.CheckConstraint(
            "(state = 'mapped' AND system_id IS NOT NULL AND reason IS NULL) OR "
            "(state = 'ignored' AND system_id IS NULL AND reason IS NOT NULL "
            " AND length(trim(reason)) > 0)",
            name='ck_discovery_reconciliation_mapped_or_ignored_with_reason',
        ),
    )
    op.create_index(
        op.f('ix_privacycare_discovery_reconciliation_stagedresource_urn'),
        'privacycare_discovery_reconciliation',
        ['stagedresource_urn'],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f('ix_privacycare_discovery_reconciliation_stagedresource_urn'),
        table_name='privacycare_discovery_reconciliation',
    )
    op.drop_table('privacycare_discovery_reconciliation')
