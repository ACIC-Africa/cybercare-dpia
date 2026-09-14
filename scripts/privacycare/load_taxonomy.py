#!/usr/bin/env python3
"""CLI wrapper around fides.api.privacycare.taxonomy.loader.

Opens one session against the same database _database_url() in
src/fides/api/privacycare/migrations/env.py resolves: PRIVACYCARE_DATABASE_URL
first if set, else the FIDES__DATABASE__* composition (same env vars, same
127.0.0.1:5442 defaults) also used by scripts/privacycare/migrate.sh — same
precedence order, so this script and the rest of PrivacyCare's tooling never
disagree about where "the database" is. Logic is duplicated rather than
imported from env.py: importing that module runs Alembic migrations as a
side effect of the import itself (module-level `context.config` access and
an unconditional `run_migrations_online()`/`run_migrations_offline()` call
at the bottom of the file, outside any `if __name__ == "__main__"` guard) —
importing it here would migrate the database as a side effect of parsing
CLI args.

Dry run is the default in both directions: with no flags it loads (and
rolls back); with --revert alone it reverts (and rolls back). --commit is
the single persistence switch — it commits whichever operation --revert
selected. --revert --commit really does persist the revert (F10); the
loader/reverter functions themselves never commit — the caller's session
boundary decides that (see loader.py's module docstring).

CHANGING THE CONTENT OF AN ALREADY-LOADED ROW (I4). The taxonomy INSERTs are
ON CONFLICT (fides_key) DO NOTHING, so editing a created row's name,
description or parent_key in kenyan.py and re-running this script does NOT
update the database — the mapping table (an upsert) would record the new
decision while ctl_data_categories / ctl_data_subjects kept the old one. The
loader now refuses to finish in that state (it reads every created key back
and raises on the difference). The remedy is two runs:

    load_taxonomy.py --revert --commit    # then
    load_taxonomy.py --commit

subject to the revert guard: --revert refuses while any privacy declaration
still names one of the created keys (the count is printed on every run as
"declarations referencing created keys"), because deleting a category a
declaration names makes Fides reject the next save of that system. --force
overrides the guard and accepts that consequence deliberately.
"""
import argparse
import os
import sys

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.taxonomy.loader import (
    LoadSummary,
    count_declarations_referencing_created_keys,
    load_kenyan_taxonomy,
    revert_kenyan_taxonomy,
)


def _database_url() -> str:
    # Copied from src/fides/api/privacycare/migrations/env.py's
    # _database_url() verbatim (same precedence, same env vars, same
    # defaults) rather than imported — see the module docstring for why
    # importing env.py isn't safe here.
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


def _print_load_summary(summary: LoadSummary) -> None:
    print(f"subjects_reused: {summary.subjects_reused}")
    print(f"subjects_created: {summary.subjects_created}")
    print(f"subjects_skipped: {summary.subjects_skipped}")
    print(f"categories_created: {summary.categories_created}")
    print(f"categories_tagged: {summary.categories_tagged}")
    print(f"categories_skipped: {summary.categories_skipped}")
    print(f"grounds_loaded: {summary.grounds_loaded}")
    print(f"grounds_unmapped: {summary.grounds_unmapped}")
    print(f"mapping rows: {summary.mapping_rows}")
    for taxonomy, term, owner in summary.deferred:
        print(f"DEFERRED  {taxonomy}  {term}  → {owner}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load (or revert) the Kenyan taxonomy into Fides' tables."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually commit the transaction (whichever operation --revert selects). "
        "Without this, the run is a dry run (rolled back, nothing written) in both "
        "directions.",
    )
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Revert the Kenyan taxonomy load instead of loading it. Dry run unless "
        "--commit is also passed.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Revert even though privacy declarations still name keys this loader "
        "created — they will be left naming keys that no longer exist, and Fides "
        "will reject the next save of those systems. Only meaningful with --revert.",
    )
    args = parser.parse_args(argv)
    if args.force and not args.revert:
        parser.error("--force is only meaningful with --revert")
    return args


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    engine = sqlalchemy.create_engine(_database_url())
    # Not `with Session(engine) as db:` — the sqlmypy plugin (still 1.x-era,
    # per pyproject.toml's [tool.mypy] plugins) doesn't see Session's
    # context-manager protocol and flags __enter__/__exit__ as missing.
    # try/finally gets the same close-on-exit guarantee without tripping it.
    db = Session(engine)
    try:
        # I3: printed on every run, dry or not, in both directions — it is
        # the number that decides whether a revert is safe, and an operator
        # should see it before running one rather than by reading a
        # traceback after.
        print(
            "declarations referencing created keys: "
            f"{count_declarations_referencing_created_keys(db)}"
        )
        if args.revert:
            revert_kenyan_taxonomy(db, force=args.force)
            print("revert_kenyan_taxonomy: done")
        else:
            summary = load_kenyan_taxonomy(db)
            _print_load_summary(summary)

        if args.commit:
            db.commit()
            print("COMMITTED")
        else:
            db.rollback()
            print("DRY RUN — nothing written")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
