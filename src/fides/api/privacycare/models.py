# PrivacyCare's own SQLAlchemy metadata.
#
# Deliberately NOT Fides' Base.metadata: keeping a separate MetaData means our
# Alembic chain can never autogenerate a drop for a Fides table, and our models
# are not registered in an Ethyca-authored module.
import uuid

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import registry

PRIVACYCARE_METADATA = MetaData()

mapper_registry = registry(metadata=PRIVACYCARE_METADATA)


def _uuid() -> str:
    return str(uuid.uuid4())


business_process_table = Table(
    "privacycare_business_process",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column("name", String(255), nullable=False),
    Column("description", Text),
    Column("business_cycle", String(255)),
    Column("owner_name", String(255)),
    Column("owner_email", String(255)),
    Column("is_critical", Boolean, nullable=False, server_default="false"),
    Column("criticality_note", Text),
    Column("external_ref", String(255)),
    Column("last_attested_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
    Column("deleted_at", DateTime(timezone=True)),
)


@mapper_registry.mapped
class BusinessProcess:
    # A business process the customer actually runs. Fides has no equivalent:
    # its map is anchored on systems, which can be scanned, while a process
    # exists only once a consultant has written it down.
    __table__ = business_process_table


process_declaration_table = Table(
    "privacycare_process_declaration",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column(
        "business_process_id",
        String(255),
        ForeignKey("privacycare_business_process.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # No FK: privacydeclaration's lifecycle belongs to upstream Fides. The read
    # path skips links whose declaration has gone.
    Column("privacy_declaration_id", String(255), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint(
        "business_process_id",
        "privacy_declaration_id",
        name="uq_privacycare_process_declaration",
    ),
)


@mapper_registry.mapped
class ProcessDeclaration:
    # The ROPA edge: this business process processes data under that
    # declaration. Projects onto edge_process_handles_data in phase 3.
    __table__ = process_declaration_table


taxonomy_mapping_table = Table(
    "privacycare_taxonomy_mapping",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    # "data_subject" | "data_category"
    Column("taxonomy", String(32), nullable=False),
    # The customer's term, verbatim, including their spelling.
    Column("customer_term", String(255), nullable=False),
    # Where the term landed. NULL when action is "dropped" or "deferred".
    Column("fides_key", String(255)),
    # "reuse" (existing default key) | "create" (new is_default=false row)
    # | "tag" (existing row, tag added) | "dropped" | "not_personal_data"
    # | "deferred"
    Column("action", String(32), nullable=False),
    Column("reason", Text, nullable=False),
    Column("source_document", String(255), nullable=False),
    # D-KT-8 facet, subjects only. NULL = not yet recorded (OQ-KT-1, Carol).
    Column("names_person_directly", Boolean),
    Column("natural_person_role", String(255)),
    Column("appears_via", String(255)),
    # Who closes a "deferred" row.
    Column("deferred_to", String(64)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("taxonomy", "customer_term", name="uq_privacycare_taxonomy_mapping_term"),
)


@mapper_registry.mapped
class TaxonomyMapping:
    # D-KT-7: the audit trail. Why "Intermediary/Agent/Independent Broker"
    # became `intermediary_broker` is a row, not a memory.
    __table__ = taxonomy_mapping_table


processing_ground_table = Table(
    "privacycare_processing_ground",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column("ground", String(255), nullable=False, unique=True),
    # The customer's own lawful_basis_class column, verbatim; may be NULL.
    Column("source_class", String(64)),
    # One of fideslang's six LegalBasisForProcessingEnum values, or NULL
    # (D-KT-4: 12 of 23 are NULL until Carol rules). The form never offers
    # a NULL-class ground.
    Column("fides_legal_basis", String(64)),
    # Where the ground's NAME makes the class obvious but the source did not
    # say. Never used by the form; exists so Carol confirms rather than types.
    Column("suggested_fides_legal_basis", String(64)),
    Column("provenance", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


@mapper_registry.mapped
class ProcessingGround:
    __table__ = processing_ground_table


declaration_ground_table = Table(
    "privacycare_declaration_ground",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    # No FK: privacydeclaration's lifecycle belongs to upstream Fides (same
    # rule as process_declaration_table).
    Column("privacy_declaration_id", String(255), nullable=False, unique=True),
    Column(
        "processing_ground_id",
        String(255),
        ForeignKey("privacycare_processing_ground.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    Column("recorded_by", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


@mapper_registry.mapped
class DeclarationGround:
    # D-KT-5: which Kenyan ground produced this declaration's Article 6 value.
    __table__ = declaration_ground_table


dsr_timeline_table = Table(
    "privacycare_dsr_timeline",
    PRIVACYCARE_METADATA,
    Column("right", String(32), primary_key=True),
    # Nullable on purpose: the brief gives objection no timeline, and inventing
    # one would decide when a controller is in breach. NULL means "unclocked",
    # which is a reportable state, not a missing value (OQ-PRIVACY-02, Carol).
    Column("days", Integer),
    Column("source_note", Text),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


@mapper_registry.mapped
class DsrTimeline:
    # How long Kenya gives a controller to answer each right. A table rather
    # than a constant because two of these values are still open questions for
    # the SME, and answering one must not require a deploy.
    __table__ = dsr_timeline_table


dsr_request_table = Table(
    "privacycare_dsr_request",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column(
        "right",
        String(32),
        ForeignKey("privacycare_dsr_timeline.right"),
        nullable=False,
        index=True,
    ),
    Column("subject_identifier", String(255), nullable=False),
    Column(
        "received_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Computed from the timeline at creation and then FROZEN: a later change to
    # the timeline table must not silently move a live obligation's deadline.
    # NULL where the right is unclocked.
    Column("deadline_at", DateTime(timezone=True)),
    Column("owner_email", String(255)),
    # D-DSR-8's fallback chain resolves to one of four values (see
    # register.resolve_owner: "explicit" | "business_process" |
    # "configured_dpo" | "unassigned") every time a request is recorded.
    # Nullable rather than NOT NULL: this column did not exist when the
    # table was created (fix round 1 on task 4), and a hand-written INSERT
    # that predates that fix, or any future write path that never calls
    # resolve_owner, must remain a legal row rather than fail a constraint
    # it cannot satisfy. There are zero live rows to backfill as of this
    # migration.
    Column("owner_source", String(32)),
    # Final review wave (minor finding): resolve_owner already reads this to
    # resolve owner_source="business_process", but the row used to drop
    # WHICH process that was, leaving half of an auditable claim unrecorded.
    # Nullable, no FK cascade (SET NULL): a deleted business process must
    # not delete or block deleting a regulatory record — the record
    # survives with the link cleared. See migration
    # c9a1e5b7d3f2_dsr_business_process_id.
    Column(
        "business_process_id",
        String(255),
        ForeignKey("privacycare_business_process.id", ondelete="SET NULL"),
        index=True,
    ),
    Column("status", String(32), nullable=False, server_default="open"),
    Column("outcome", String(32)),
    Column("outcome_grounds", Text),
    Column("decided_by", String(255)),
    Column("decided_at", DateTime(timezone=True)),
    Column("subject_notified_at", DateTime(timezone=True)),
    # No FK: privacyrequest's lifecycle belongs to upstream Fides, same reason
    # process_declaration_table carries no FK to privacydeclaration. NULL for
    # restriction and objection, which move no data and so have no Fides side.
    Column("fides_privacy_request_id", String(255), index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


@mapper_registry.mapped
class DsrRequest:
    # A Kenyan data-subject right the customer owes an answer to. Fides has a
    # privacy request, but it is anchored on execution — it exists to move data
    # across integrations. Two of Kenya's six rights move no data at all, and
    # Fides has no owner field to alert, which is why the obligation lives here
    # and only the data movement is delegated (spec D-DSR-1, Barbara 2026-09-15).
    __table__ = dsr_request_table


dsr_alert_table = Table(
    "privacycare_dsr_alert",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column(
        "dsr_request_id",
        String(255),
        ForeignKey("privacycare_dsr_request.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("kind", String(32), nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("channel", String(32), nullable=False),
    Column("recipient", String(255)),
    UniqueConstraint("dsr_request_id", "kind", name="uq_privacycare_dsr_alert"),
)


@mapper_registry.mapped
class DsrAlert:
    # One row per alert actually delivered. This table IS the once-only
    # guarantee D-DSR-5 demands FOR THE LEDGER: a flag in the request row
    # would lose to a second worker, a restart mid-run, or a retry, so the
    # unique constraint on (dsr_request_id, kind) lives here instead. I2
    # (final review of plan 15): that is the row's guarantee, not
    # delivery's — a second worker or a mid-run restart racing the same
    # alert can still re-deliver the underlying MESSAGE before this row is
    # written; only the second INSERT is stopped, degrading to a no-op
    # rather than a duplicate row. An owner who gets the same warning twice
    # occasionally is a smaller failure than a channel muted by a genuinely
    # repeating alert, which is what this table exists to prevent.
    __table__ = dsr_alert_table


consent_rule_table = Table(
    "privacycare_consent_rule",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column("rule", String(64), nullable=False),
    Column("source_note", Text),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


@mapper_registry.mapped
class ConsentRule:
    # What counts as a notice change material enough to invalidate consent
    # already given. A table rather than a constant because the answer is an
    # open question for the SME (OQ-CON-01), and answering it must not
    # require a deploy — the same reason privacycare_dsr_timeline is a table.
    __table__ = consent_rule_table


dpia_risk_table = Table(
    "privacycare_dpia_risk",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    # References privacy_assessment.id, an ETHYCA table. Deliberately NO
    # ForeignKey: a constraint from our chain into theirs is the coupling
    # that breaks an upstream merge. Existence is checked at write time.
    Column("assessment_id", String(255), nullable=False, index=True),
    Column("category", String(64), nullable=False),
    Column("description", Text, nullable=False),
    Column("likelihood", Integer, nullable=False),
    Column("severity", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
    CheckConstraint("likelihood BETWEEN 1 AND 5", name="ck_dpia_risk_likelihood"),
    CheckConstraint("severity BETWEEN 1 AND 5", name="ck_dpia_risk_severity"),
)


@mapper_registry.mapped
class DpiaRisk:
    # One entry in a DPIA's risk register. It exists because Fides has
    # nowhere to put it: assessment_question carries no numeric column, so
    # a likelihood and a severity cannot be modelled as questions. Same
    # reason privacycare_business_process exists.
    #
    # score and band are NOT stored. They are computed from likelihood and
    # severity on read, so a change to the banding rule cannot leave stale
    # numbers behind in a compliance record.
    __table__ = dpia_risk_table


screening_trigger_table = Table(
    "privacycare_screening_trigger",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    Column("trigger_key", String(64), nullable=False, unique=True),
    Column("label", String(255), nullable=False),
    Column("description", Text, nullable=False),
    Column("display_order", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


@mapper_registry.mapped
class ScreeningTrigger:
    # The questions a screener answers before a DPIA exists. A table rather
    # than a constant because the wording belongs to the privacy SME and she
    # will revise it — two of the six were already recast once, on
    # 2026-09-17, from a university sample case to this customer's world.
    # Same shape and same reason as privacycare_dsr_timeline.
    __table__ = screening_trigger_table


screening_decision_table = Table(
    "privacycare_screening_decision",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    # References privacydeclaration.id, an ETHYCA table. Deliberately NO
    # ForeignKey: a constraint from our chain into theirs is the coupling
    # that breaks an upstream merge. Existence is checked at write time.
    # The declaration is this platform's processing activity — see
    # context.py's GenerationTarget docstring.
    Column("declaration_id", String(255), nullable=False, index=True),
    Column("dpia_required", Boolean, nullable=False),
    # Which triggers were ticked. Empty exactly when dpia_required is false.
    Column("triggered_keys", ARRAY(String), nullable=False),
    # Mandatory when dpia_required is false: WHY no assessment is needed.
    # This is the compliance artifact a regulator asks for when they ask
    # why an activity has none.
    Column("justification", Text),
    Column("decided_by", String(255), nullable=False),
    Column(
        "decided_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
    CheckConstraint(
        "(dpia_required AND justification IS NULL) OR "
        "(NOT dpia_required AND justification IS NOT NULL "
        " AND length(trim(justification)) > 0)",
        name="ck_screening_screenout_has_a_reason",
    ),
)


@mapper_registry.mapped
class ScreeningDecision:
    # One screening verdict for one processing activity, at one moment.
    # APPEND-ONLY: re-screening writes a new row and never updates an old
    # one. An activity screened out last quarter may need a DPIA this one,
    # and the earlier decision is the evidence of what was decided and on
    # what basis — destroying it destroys exactly what a regulator would
    # ask to see. Latest decided_at wins on read.
    __table__ = screening_decision_table


generation_skip_table = Table(
    "privacycare_generation_skip",
    PRIVACYCARE_METADATA,
    Column("id", String(255), primary_key=True, default=_uuid),
    # References privacy_assessment_task.id, an ETHYCA table. Deliberately NO
    # ForeignKey: a constraint from our chain into theirs is the coupling
    # that breaks an upstream merge. One row per task, enforced by the
    # unique index.
    Column("task_id", String(255), nullable=False, unique=True),
    Column("skipped_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


@mapper_registry.mapped
class GenerationSkip:
    # How many activities a generation run skipped because the screening
    # gate said no DPIA was needed. It lives here because
    # privacy_assessment_task is Ethyca's and has no field for it, and
    # adding a column to their table is the merge-hostile move this fork
    # avoids. Same reason privacycare_business_process and
    # privacycare_dpia_risk exist.
    __table__ = generation_skip_table
