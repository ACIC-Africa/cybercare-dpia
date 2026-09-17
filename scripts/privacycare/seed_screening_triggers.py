#!/usr/bin/env python3
"""Seed Carol's six screening triggers (spec 2026-09-16 D-W2-7, plan 18
Task 1) into privacycare_screening_trigger.

Without this seed, the table plan 18's migration creates
(54c8fce54023_screening.py) has a schema and zero rows — the same
"exists in the schema, inert in the deployment" gap every other
PrivacyCare seed script in this package exists to close. Before a DPIA is
generated, someone answers whether the activity needs one at all; the six
questions a screener answers are these rows, and with none seeded there is
nothing to screen against.

**Carol's wording is transcribed, not authored.** The six trigger_key/
label/description values below are copied character for character from
her 2026-09-17 ruling (the plan's "Carol's six triggers, as she ruled
them" table). Two of the six (special_category, vulnerable_subjects) are
recasts of a university sample case into this customer's world that she
accepted as written that day; the other four are unchanged from her
prototype. A phrase that reads awkwardly here is not this script's call to
fix — a silent improvement to an SME's text is how a compliance
instrument stops meaning what its author meant.

Same shape as scripts/privacycare/seed_kenya_template.py: one argparse
CLI, one session, dry-run by default, --commit to persist. _database_url()
and _target_description() are copied verbatim from that script (itself
copied from seed_dsr.py, seed_connection.py, import_processes.py, and
ultimately migrations/env.py) rather than imported — see
load_taxonomy.py's module docstring for why importing env.py isn't safe
here (it runs Alembic migrations as a side effect of the import itself).

Secrets come from the environment at run time and are never written into
the repo or printed — main() only ever prints the target: line (host/
port/database, never the password, never the raw URL).

**Raw SQL, no ORM writes.** Same reason seed_kenya_template.py and
dsr/timelines.py's seed_timelines are raw SQL: Fides' base_class.persist_obj
commits unconditionally, so an ORM .create() here would make a dry run
durable regardless of --commit. INSERT ... ON CONFLICT (trigger_key) DO
NOTHING is the idempotency mechanism — a second run, with or without
--commit, writes nothing new, matching seed_timelines' own idiom exactly.

seed_screening_triggers(db) never calls db.commit() or db.rollback() — the
caller's session boundary decides, the same rule every other PrivacyCare
seed function follows.
"""
import argparse
import os
import sys
import uuid

import sqlalchemy
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# Carol's six triggers, as ruled 2026-09-17. Order here is display_order
# (1-indexed, no gaps) — it mirrors the plan's own enumeration order and
# nothing downstream needs it to be anything else.
#
# Trigger 2 (special_category) matters more than it looks: the university
# original read "Health, biometric, genetic, financial" — GDPR Article 9
# shaped. Her list is DPA 2019 section 2's 28 types, and HIV status inside
# an employee wellness programme is exactly the case that must force a
# full DPIA and would have slipped past the old wording.
SCREENING_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "large_scale",
        "Large-scale processing",
        "High volume of data subjects, records, or geographic spread.",
    ),
    (
        "special_category",
        "Special category or highly sensitive data",
        "Any of the special categories under the Data Protection Act 2019 "
        "§2 — including health and HIV status, biometric and genetic "
        "data, conscience, belief, and well-being.",
    ),
    (
        "systematic_monitoring",
        "Systematic monitoring",
        "Ongoing observation, tracking, or profiling of individuals.",
    ),
    (
        "new_technology",
        "New or unproven technology",
        "AI/ML, biometric matching, or a system not previously deployed "
        "at this scale.",
    ),
    (
        "automated_decision",
        "Automated decision-making with legal or similarly significant "
        "effect",
        "Decisions made about a person with no meaningful human review.",
    ),
    (
        "vulnerable_subjects",
        "Processing involving vulnerable data subjects",
        "Minors, dependants, job applicants, individuals captured on "
        "CCTV, witnesses, complainants, suspected offenders, or anyone "
        "in a relationship of economic dependence on the company.",
    ),
)


def _database_url() -> str:
    # Copied from scripts/privacycare/seed_kenya_template.py's
    # _database_url() verbatim (same precedence, same env vars, same
    # defaults) rather than imported — see load_taxonomy.py's module
    # docstring for why importing env.py isn't safe here.
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


_SEED_SQL = sqlalchemy.text(
    "INSERT INTO privacycare_screening_trigger "
    "(id, trigger_key, label, description, display_order) "
    "VALUES (:id, :trigger_key, :label, :description, :display_order) "
    "ON CONFLICT (trigger_key) DO NOTHING"
)


def seed_screening_triggers(db: Session) -> int:
    """Idempotent on trigger_key: ON CONFLICT DO NOTHING means a rerun
    writes nothing and reports 0, the same idiom dsr/timelines.py's
    seed_timelines uses. Never commits — the caller's session boundary
    decides, matching every other PrivacyCare seed function's own module
    docstring. Returns the number of rows actually inserted.
    """
    written = 0
    for display_order, (trigger_key, label, description) in enumerate(
        SCREENING_TRIGGERS, start=1
    ):
        result = db.execute(
            _SEED_SQL,
            {
                "id": str(uuid.uuid4()),
                "trigger_key": trigger_key,
                "label": label,
                "description": description,
                "display_order": display_order,
            },
        )
        written += result.rowcount
    return written


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed Carol's six screening triggers "
        "(privacycare_screening_trigger) verbatim."
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
    # Not `with Session(engine) as db:` — the sqlmypy plugin (still
    # 1.x-era, per pyproject.toml's [tool.mypy] plugins) doesn't see
    # Session's context-manager protocol and flags __enter__/__exit__ as
    # missing. try/finally gets the same close-on-exit guarantee without
    # tripping it.
    db = Session(engine)
    try:
        try:
            triggers_written = seed_screening_triggers(db)
        except SQLAlchemyError as exc:
            # A connection failure or a database error raised while
            # seeding. NEVER print str(exc) or the raw database_url: DBAPI
            # driver error text can itself embed the DSN, credentials
            # included, on some drivers — the same leak route
            # seed_consent_demo.py's own handler guards against. Only the
            # exception's class name and the already-safe target line's
            # contents (host/port/db, never the password) go to stderr.
            try:
                db.rollback()
            except SQLAlchemyError:
                pass
            print(
                f"error: database operation failed against "
                f"{_target_description(database_url)} "
                f"({type(exc).__name__}) — nothing written",
                file=sys.stderr,
            )
            return 1

        print(f"triggers_written: {triggers_written}")

        if args.commit:
            try:
                db.commit()
            except SQLAlchemyError as exc:
                print(
                    f"error: commit failed against "
                    f"{_target_description(database_url)} "
                    f"({type(exc).__name__})",
                    file=sys.stderr,
                )
                return 1
            print("COMMITTED")
        else:
            db.rollback()
            print("DRY RUN — nothing written")
    finally:
        try:
            db.close()
        except SQLAlchemyError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
