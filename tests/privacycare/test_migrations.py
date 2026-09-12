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
    from alembic.autogenerate import compare_metadata
    from alembic.runtime import migration

    import fides.api.privacycare  # noqa: F401 -- activates fides_exclusion_guard
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


def test_excluded_tables_holds_no_privacycare_entries():
    """Regression test for a data-destruction hazard a previous fix of C1
    introduced: `fides.api.db.database.EXCLUDED_TABLES` is dual-purpose —
    `include_object` (database.py:44) reads it to filter autogenerate, but
    `reset_db` (database.py:120-123) ALSO iterates it and runs
    `DROP TABLE IF EXISTS {name} CASCADE` for every member. An earlier
    version of fides_exclusion_guard registered our `privacycare_*` table
    names into that set to close the autogenerate gap, which meant
    `fides db reset` would CASCADE-drop every PrivacyCare table on any
    process that had imported this package. The fix wraps
    `include_object` instead of touching the set (see
    fides_exclusion_guard.py's docstring). This test reads EXCLUDED_TABLES
    directly — after privacycare has been imported — and asserts no member
    starts with 'privacycare_', so nobody can reintroduce the destructive
    version without this test catching it.
    """
    import fides.api.privacycare  # noqa: F401 -- ensure the guard has run
    from fides.api.db.database import EXCLUDED_TABLES

    privacycare_entries = sorted(
        name for name in EXCLUDED_TABLES if name.startswith("privacycare_")
    )
    assert privacycare_entries == [], (
        "privacycare_* table names were found in EXCLUDED_TABLES: "
        f"{privacycare_entries} — this would make `fides db reset` "
        "CASCADE-drop PrivacyCare tables (database.py:120-123)"
    )
    assert EXCLUDED_TABLES == {
        "post_upgrade_background_migration_tasks",
        "privacy_preferences_current",
        "privacy_preferences_historic",
    }, (
        "EXCLUDED_TABLES no longer matches Fides' original three shipped "
        f"entries: {sorted(EXCLUDED_TABLES)}"
    )


def test_fides_exclusion_guard_install_is_idempotent():
    """The guard wraps `fides.api.db.database.include_object` by rebinding
    the module attribute on import. If that install step were not
    idempotent, importing the guard twice (or a reload) would treat the
    already-wrapped function as "the original" and wrap it again — each
    layer still correct in isolation, but silently losing the true Ethyca
    original underneath a growing pile of our own wrappers, and doing
    needless extra work on every autogenerate call forever. This proves
    that invoking the guard's install step twice leaves exactly one layer
    of wrapping in place: the true original stays directly reachable via
    `__privacycare_original__`, the wrapper object itself is unchanged by
    the second call, and the filter still behaves correctly (accepts
    non-privacycare tables, rejects privacycare_* tables) without
    recursing.
    """
    import fides.api.privacycare  # noqa: F401 -- ensure the guard has run at least once
    from fides.api.db import database as fides_database
    from fides.api.privacycare import fides_exclusion_guard

    before = fides_database.include_object
    assert getattr(before, "__privacycare_wrapped__", False) is True, (
        "include_object was not wrapped by fides_exclusion_guard"
    )
    true_original = before.__privacycare_original__
    assert not getattr(true_original, "__privacycare_wrapped__", False), (
        "the captured 'original' is itself one of our wrappers — a prior "
        "install() call already double-wrapped include_object"
    )

    # Invoke the install step a second time.
    fides_exclusion_guard._install()

    after = fides_database.include_object
    assert after is before, (
        "a second install() call replaced the wrapper instead of "
        "no-op'ing — include_object was wrapped again"
    )
    assert after.__privacycare_original__ is true_original, (
        "a second install() call rebound __privacycare_original__ — it "
        "would now point at the first wrapper instead of the true Ethyca "
        "original, meaning a third install() could stack indefinitely"
    )

    # The filter still behaves correctly and a single call returns
    # promptly (does not recurse).
    assert (
        after(
            object=None,
            name="privacycare_business_process",
            type_="table",
            reflected=False,
            compare_to=None,
        )
        is False
    )
    assert (
        after(
            object=None,
            name="some_other_fides_table",
            type_="table",
            reflected=False,
            compare_to=None,
        )
        is True
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
