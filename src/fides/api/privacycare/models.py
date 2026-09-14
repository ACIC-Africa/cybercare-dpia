# PrivacyCare's own SQLAlchemy metadata.
#
# Deliberately NOT Fides' Base.metadata: keeping a separate MetaData means our
# Alembic chain can never autogenerate a drop for a Fides table, and our models
# are not registered in an Ethyca-authored module.
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
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
