# The `include_object` filter PrivacyCare's own Alembic chain runs
# autogenerate through, plus the constants it depends on.
#
# Pulled out of env.py (rather than left inline) so it can be imported
# directly by tests without triggering env.py's module-level
# `run_migrations_offline()/run_migrations_online()` call, which requires a
# live `alembic.context` proxy that only exists inside an actual Alembic
# invocation. See tests/privacycare/test_migrations.py for the regression
# test (C2) that re-measures the "0 diff ops" guarantee this filter makes.
VERSION_TABLE = "privacycare_alembic_version"
TABLE_PREFIX = "privacycare_"


def include_object(obj, name, type_, reflected, compare_to):
    # WITHOUT THIS FILTER, AUTOGENERATE DROPS THE FIDES DATABASE.
    #
    # Autogenerate compares the live database against target_metadata and
    # proposes removing anything it cannot see. PRIVACYCARE_METADATA holds only
    # our tables, so every Fides table looks removable. Measured against the
    # live database on 2026-09-11: 161 remove_table and 441 remove_index ops.
    # With this filter: 0. This measurement is re-taken on every test run by
    # test_migrations.py's C2 test rather than trusted as a one-time claim.
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
