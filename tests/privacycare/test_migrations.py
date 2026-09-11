# PrivacyCare runs its own migration chain so that upstream Fides upgrades
# never collide with ours. Capture cannot happen retroactively inside a test
# (we don't control what ran before us), so these tests prove non-tangling
# structurally instead of by snapshotting a "before" value:
#   1. our chain applies (privacycare_alembic_version exists, one row);
#   2. Fides' alembic_version and PrivacyCare's privacycare_alembic_version
#      hold DIFFERENT revision ids (a shared head would mean the two chains
#      are tangled);
#   3. Fides' current revision id is NOT among the revision ids declared in
#      src/fides/api/privacycare/migrations/versions/*.py (this is the
#      assertion that would actually catch our chain having stamped Fides'
#      table with one of our own revisions);
#   4. PrivacyCare's current revision id IS among those declared ids
#      (otherwise a version_table misconfigured to point at the wrong table
#      would silently pass).
import os
import re

import sqlalchemy

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSIONS_DIR = os.path.join(
    REPO_ROOT, "src", "fides", "api", "privacycare", "migrations", "versions"
)

_REVISION_RE = re.compile(r"^revision\s*=\s*['\"]([^'\"]+)['\"]")


def _engine():
    return sqlalchemy.create_engine(
        "postgresql://postgres:fides@127.0.0.1:5442/fides"
    )


def _declared_privacycare_revisions():
    """Revision ids declared by `revision = "..."` in every PrivacyCare
    migration script. Parsed from source rather than imported so this stays
    a static, side-effect-free check."""
    revisions = set()
    for filename in os.listdir(VERSIONS_DIR):
        if not filename.endswith(".py"):
            continue
        path = os.path.join(VERSIONS_DIR, filename)
        with open(path, "r") as f:
            for line in f:
                match = _REVISION_RE.match(line)
                if match:
                    revisions.add(match.group(1))
                    break
    return revisions


def test_privacycare_version_table_exists():
    inspector = sqlalchemy.inspect(_engine())
    assert "privacycare_alembic_version" in inspector.get_table_names(), (
        "PrivacyCare's migration chain has not been applied"
    )


def test_fides_and_privacycare_chains_are_separate():
    inspector = sqlalchemy.inspect(_engine())
    names = set(inspector.get_table_names())
    assert "alembic_version" in names, "Fides' own version table should still exist"
    assert "privacycare_alembic_version" in names
    with _engine().connect() as conn:
        fides_rows = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM alembic_version")
        ).scalar_one()
        pc_rows = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM privacycare_alembic_version")
        ).scalar_one()
        fides_revision = conn.execute(
            sqlalchemy.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        pc_revision = conn.execute(
            sqlalchemy.text("SELECT version_num FROM privacycare_alembic_version")
        ).scalar_one()
    assert fides_rows == 1, "Fides' chain must still have exactly one head"
    assert pc_rows == 1, "PrivacyCare's chain must have exactly one head"

    assert fides_revision != pc_revision, (
        "Fides' and PrivacyCare's version tables hold the same revision id — "
        "the two chains are tangled"
    )

    declared = _declared_privacycare_revisions()
    assert fides_revision not in declared, (
        "Fides' alembic_version holds a revision id declared in PrivacyCare's "
        "migration chain — our chain has stamped Fides' table"
    )
    assert pc_revision in declared, (
        "privacycare_alembic_version holds a revision id not declared in "
        "PrivacyCare's migration chain — version_table may be misconfigured "
        "and pointing at the wrong table"
    )


def test_c1_fides_autogenerate_would_not_drop_privacycare_tables():
    # C1: the guard above runs in one direction only. Nobody checked the
    # reverse: Fides' own `alembic revision --autogenerate`
    # (src/fides/api/alembic/) compares the live database against
    # `fides.api.db.base.Base.metadata` through
    # `fides.api.db.database.include_object`, which excludes only the table
    # names listed in `fides.api.db.database.EXCLUDED_TABLES` — three names,
    # none of them ours (read from database.py, not guessed). PrivacyCare's
    # tables are not in that set and are not part of Base.metadata, so
    # without protection this comparison sees every privacycare_* table as
    # removable and Fides' own autogenerate would emit
    # `op.drop_table('privacycare_business_process')` the moment anyone
    # adds a Fides-side model.
    #
    # We may never edit database.py (Ethyca-authored). EXCLUDED_TABLES is a
    # plain, mutable `set` object, though, and include_object re-checks
    # membership against it at call time rather than freezing a copy at
    # import time — so fides.api.privacycare.fides_exclusion_guard
    # registers our table names into that same set object from our own
    # side, on import. Importing fides.api.privacycare below (as every
    # other test in this package already does transitively) runs that
    # guard. This test proves it actually closes the gap against a live
    # database, using Fides' real include_object function — not a
    # reimplementation that could drift from it.
    #
    # RESIDUAL RISK (see fides_exclusion_guard.py for the full docstring):
    # the guard protects only a process that has imported
    # fides.api.privacycare before Fides' autogenerate runs. Nothing under
    # src/fides/ outside privacycare/ imports this package today, so a bare
    # `fides db generate-migration` invoked from a process that never
    # touches PrivacyCare code is NOT yet protected by this alone.
    import fides.api.privacycare  # noqa: F401 -- activates fides_exclusion_guard
    from alembic.autogenerate import compare_metadata
    from alembic.runtime import migration

    from fides.api.db.base import Base
    from fides.api.db.database import include_object as fides_include_object

    with _engine().connect() as connection:
        migration_context = migration.MigrationContext.configure(
            connection, opts={"include_object": fides_include_object}
        )
        diff = compare_metadata(migration_context, Base.metadata)

    dropped_privacycare_tables = sorted(
        op[1].name
        for op in diff
        if op[0] == "remove_table" and op[1].name.startswith("privacycare_")
    )
    assert dropped_privacycare_tables == [], (
        "Fides' own autogenerate would drop PrivacyCare tables: "
        f"{dropped_privacycare_tables} — fides_exclusion_guard did not "
        "close the gap"
    )


def test_c2_privacycare_autogenerate_measures_zero_diff_ops():
    # C2: env.py's "0 diff ops" claim was a comment recording a manual
    # measurement taken on 2026-09-11, never re-checked by a test. An
    # Alembic upgrade or a new model could silently void it. This
    # re-measures it on every run, against the same include_object function
    # PrivacyCare's own Alembic chain actually runs through (pulled out of
    # env.py into include_object.py specifically so it can be imported
    # without triggering env.py's live migration run — see that module).
    #
    # Also closes a separate blind spot: if a future PrivacyCare table were
    # ever added to PRIVACYCARE_METADATA without the "privacycare_" prefix,
    # include_object's prefix filter would silently treat it as a Fides
    # table and it would never be created. Asserting every table in
    # PRIVACYCARE_METADATA.tables is prefixed catches that before it ships.
    from alembic.autogenerate import compare_metadata
    from alembic.runtime import migration

    from fides.api.privacycare.migrations.include_object import (
        VERSION_TABLE,
        include_object,
    )
    from fides.api.privacycare.models import PRIVACYCARE_METADATA

    unprefixed = [
        name
        for name in PRIVACYCARE_METADATA.tables
        if not name.startswith("privacycare_")
    ]
    assert unprefixed == [], (
        f"table(s) {unprefixed} in PRIVACYCARE_METADATA do not start with "
        "'privacycare_' — include_object's prefix filter would silently "
        "skip them and they would never be created"
    )

    with _engine().connect() as connection:
        migration_context = migration.MigrationContext.configure(
            connection,
            opts={"version_table": VERSION_TABLE, "include_object": include_object},
        )
        diff = compare_metadata(migration_context, PRIVACYCARE_METADATA)

    remove_ops = [op for op in diff if op[0] in ("remove_table", "remove_index")]
    assert remove_ops == [], (
        "PrivacyCare's own autogenerate view of the live database is not "
        f"clean (0 diff ops expected): "
        f"{[(op[0], getattr(op[1], 'name', op[1])) for op in remove_ops]}"
    )
