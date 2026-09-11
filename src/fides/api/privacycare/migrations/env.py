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

from fides.api.privacycare.migrations.include_object import (
    VERSION_TABLE,
    include_object,
)
from fides.api.privacycare.models import PRIVACYCARE_METADATA

config = context.config


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
