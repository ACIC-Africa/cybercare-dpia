# Alembic environment for PrivacyCare's chain.
#
# Two things make this chain independent of Fides':
#   1. version_table is privacycare_alembic_version, so the two chains never
#      read or write each other's head.
#   2. target_metadata is PrivacyCare's own MetaData, so autogenerate never
#      proposes dropping a Fides table it cannot see.
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from fides.api.privacycare.models import PRIVACYCARE_METADATA

config = context.config

VERSION_TABLE = "privacycare_alembic_version"
TABLE_PREFIX = "privacycare_"


def include_object(obj, name, type_, reflected, compare_to):
    # WITHOUT THIS FILTER, AUTOGENERATE DROPS THE FIDES DATABASE.
    #
    # Autogenerate compares the live database against target_metadata and
    # proposes removing anything it cannot see. PRIVACYCARE_METADATA holds only
    # our tables, so every Fides table looks removable. Measured against the
    # live database on 2026-09-11: 161 remove_table and 441 remove_index ops.
    # With this filter: 0.
    if type_ == "table":
        return bool(name) and name.startswith(TABLE_PREFIX)
    if type_ == "index" and getattr(obj, "table", None) is not None:
        return obj.table.name.startswith(TABLE_PREFIX)
    # Fallthrough: column, unique_constraint, foreign_key_constraint (and an
    # index whose obj.table is None) are allowed through with no prefix
    # check. That is safe only structurally: PRIVACYCARE_METADATA contains no
    # Fides tables, so the table-level exclusion above always short-circuits
    # before any per-column/constraint comparison is ever run against a
    # Fides table in the first place. This invariant depends on
    # PRIVACYCARE_METADATA never containing a Fides table — if that ever
    # changes, this fallthrough must be revisited.
    return True


def _database_url() -> str:
    return os.environ.get(
        "PRIVACYCARE_DATABASE_URL",
        "postgresql://{u}:{p}@{h}:{port}/{db}".format(
            u=os.environ.get("FIDES__DATABASE__USER", "postgres"),
            p=os.environ.get("FIDES__DATABASE__PASSWORD", "fides"),
            h=os.environ.get("FIDES__DATABASE__SERVER", "127.0.0.1"),
            port=os.environ.get("FIDES__DATABASE__PORT", "5442"),
            db=os.environ.get("FIDES__DATABASE__DB", "fides"),
        ),
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=PRIVACYCARE_METADATA,
        version_table=VERSION_TABLE,
        include_object=include_object,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _database_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=PRIVACYCARE_METADATA,
            version_table=VERSION_TABLE,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
