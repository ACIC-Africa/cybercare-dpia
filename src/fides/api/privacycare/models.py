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
