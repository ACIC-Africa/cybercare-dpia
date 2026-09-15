#!/usr/bin/env python3
"""Seed the DSR register's runtime prerequisites: the six Kenyan timeline
rows, and the three Fides policies (+ rules) the data-moving rights delegate
to.

C1 (final review, plan 14): `seed_timelines` (dsr/timelines.py) and
`ensure_kenyan_policies` (dsr/delegation.py) were being called from tests
only — `grep -rn "ensure_kenyan_policies\\|seed_timelines" src/ tests/`
before this script existed proved it. With neither ever run against the
live database, `privacycare_dsr_timeline` held zero rows and no
`privacycare_kenya_*` policy existed, so every POST to
`/api/v1/privacycare/dsr-requests` returned 400 for all six rights (an
unseeded-database ValueError out of record_request), and once timelines
existed the three delegating rights would still 400 for want of a policy
(delegate's own "no Fides policy ... call ensure_kenyan_policies() before
delegating" ValueError). The feature was inert in the deployed system.

Same shape as scripts/privacycare/seed_connection.py: one argparse CLI, one
session, dry-run by default, `--commit` to persist. `_database_url()` and
`_target_description()` are copied verbatim from seed_connection.py (itself
copied from import_processes.py, itself copied from migrations/env.py)
rather than imported — see load_taxonomy.py's module docstring for why
importing env.py isn't safe here (it runs Alembic migrations as a side
effect of the import itself).

Secrets come from the environment at run time and are never written into
the repo or printed — `main()` only ever prints the `target:` line
(host/port/database, never the password, never the raw URL).

`seed_dsr(db)` never commits — the caller's session boundary decides, same
rule as seed_timelines and ensure_kenyan_policies themselves (see their own
module docstrings in dsr/timelines.py and dsr/delegation.py). Both are
idempotent (ON CONFLICT DO NOTHING for timelines; Policy/Rule.create_or_update
for policies), so a second run of this script is a no-op that reports zero
timeline rows written and the same policy timeframes back.
"""
import argparse
import os
import sys

import sqlalchemy
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

import fides.api.db.base  # noqa: F401 — see below
from fides.api.privacycare.dsr.delegation import ensure_kenyan_policies
from fides.api.privacycare.dsr.timelines import seed_timelines

# `fides.api.db.base` is imported (but not from) purely for its side effect:
# it is Alembic's single central import point that pulls in every mapped
# SQLAlchemy class before any of them are used. ensure_kenyan_policies
# touches Policy and Rule (fides.api.models.policy) directly through the
# ORM, and without this import SQLAlchemy can fail to resolve their
# relationships the first time the mapper configures, because some related
# class was never imported and registered — the exact failure mode
# seed_connection.py's own docstring documents for ConnectionConfig.


def _database_url() -> str:
    # Copied from scripts/privacycare/seed_connection.py's _database_url()
    # verbatim (same precedence, same env vars, same defaults) rather than
    # imported — see load_taxonomy.py's module docstring for why importing
    # env.py isn't safe here.
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


def _target_description(database_url: str) -> str:
    """Render `database_url` as `user@host:port/db` for the pre-write
    target line. NEVER includes the password, and never prints the raw
    URL — a URL with embedded credentials is exactly what a consultant
    should not paste into a terminal transcript or a ticket.
    """
    parsed = make_url(database_url)
    user = parsed.username or ""
    location = parsed.host or ""
    if parsed.port:
        location = f"{location}:{parsed.port}"
    database = parsed.database or ""
    return f"{user}@{location}/{database}"


def seed_dsr(db: Session) -> dict:
    """Seed the six timeline rows, then provision the three Kenyan
    policies — in that order, because ensure_kenyan_policies reads the
    timeline table (timeline_days) to set each policy's
    execution_timeframe, and would raise on a still-unseeded database.

    This function itself never calls db.commit() or db.rollback() — the
    caller's session boundary decides, matching seed_timelines and
    ensure_kenyan_policies' own module docstrings. Returns a summary dict:
    {"timeline_rows_written": int, "policies": {policy_key: days}}.

    CALLER TRAP, discovered running this script for real (not just under
    the test suite's monkeypatched db fixture): ensure_kenyan_policies
    writes Policy/Rule rows through Fides' own ORM
    (Policy.create_or_update / Rule.create_or_update), and Fides'
    OrmWrappedFidesBase.persist_obj (src/fides/api/db/base_class.py) calls
    db.commit() UNCONDITIONALLY as part of every create/update — it is not
    optional, and this function has no way to suppress it. Every test that
    exercises ensure_kenyan_policies monkeypatches session.commit to
    session.flush, which silently absorbs that inner commit and makes it
    look like ensure_kenyan_policies "never commits" — but a real,
    unmocked session has no such patch, and that inner db.commit() ends
    the real transaction outright. A caller that wraps this in what it
    believes is a dry run (do the writes, then db.rollback() instead of
    db.commit()) discovers the rollback has nothing left to undo: the
    Policy/Rule rows are already permanently on disk the moment
    ensure_kenyan_policies returns.

    A SAVEPOINT (db.begin_nested()) around the call looks like the fix but
    is not: persist_obj calls db.commit() and THEN db.refresh(resource) in
    the same method, and that refresh raises "Can't operate on closed
    transaction inside context manager" once the inner commit has already
    released the savepoint out from under the enclosing `with
    db.begin_nested():` block — verified by actually running it, not
    reasoned about in the abstract. See main() below for what actually
    works: the exact same monkeypatch every test fixture in this package
    already uses (session.commit -> session.flush) for the duration of the
    seeding call, restored before the caller's own real commit/rollback
    decides the outcome.
    """
    timeline_rows_written = seed_timelines(db)
    policies = ensure_kenyan_policies(db)
    return {
        "timeline_rows_written": timeline_rows_written,
        "policies": policies,
    }


def _print_summary(summary: dict) -> None:
    print(f"timeline_rows_written: {summary['timeline_rows_written']}")
    print("policies (key -> execution_timeframe days):")
    for key, days in sorted(summary["policies"].items()):
        print(f"  {key}: {days}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the DSR register's runtime prerequisites: the six "
        "Kenyan timeline rows (privacycare_dsr_timeline) and the three "
        "delegating rights' Fides policies (privacycare_kenya_access/"
        "erasure/portability)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually commit the transaction. Without this, the run is a "
        "dry run (rolled back, nothing written).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    database_url = _database_url()
    # Name the target BEFORE opening a session or writing anything, so a
    # consultant pointing this at the wrong database finds out immediately.
    # Never the password, never the raw URL.
    print(f"target: {_target_description(database_url)}")

    engine = sqlalchemy.create_engine(database_url)
    # Not `with Session(engine) as db:` — the sqlmypy plugin (still 1.x-era,
    # per pyproject.toml's [tool.mypy] plugins) doesn't see Session's
    # context-manager protocol and flags __enter__/__exit__ as missing.
    # try/finally gets the same close-on-exit guarantee without tripping it.
    db = Session(engine)
    try:
        # See seed_dsr()'s docstring for why this is here at all:
        # ensure_kenyan_policies' Policy/Rule writes go through Fides' own
        # ORM, whose persist_obj calls db.commit() unconditionally — with
        # no patch, that inner commit would end the real transaction the
        # moment seed_dsr returns, making everything below (including a
        # plain "dry run" with no --commit) a real, permanent write
        # regardless of args.commit. Absorbing db.commit() into db.flush()
        # for the duration of the seeding call is the exact pattern every
        # test fixture in this package already relies on
        # (`monkeypatch.setattr(session, "commit", session.flush)`) —
        # restored immediately after (success or failure alike), so the
        # REAL db.commit()/db.rollback() below is what actually decides
        # whether anything survives.
        real_commit = db.commit
        db.commit = db.flush  # type: ignore[method-assign]
        try:
            summary = seed_dsr(db)
        except ValueError as exc:
            db.commit = real_commit  # type: ignore[method-assign]
            db.rollback()
            print(str(exc), file=sys.stderr)
            return 1
        db.commit = real_commit  # type: ignore[method-assign]

        _print_summary(summary)

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
