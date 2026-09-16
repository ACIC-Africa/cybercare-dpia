#!/usr/bin/env python3
"""Seed the Kenya DPA 2019 DPIA template (spec 2026-09-16 D-W2-2, Task 5).

assessment_template holds 13 rows today (checked directly against the live
database — see the GDPR_TEMPLATE_ID comment below for the full list); none
is region='Kenya'. Without this seed, api/risk.py's register (Tasks 1-4)
has DPIAs to score but no Kenyan questionnaire to score them against — the
same "exists in the schema, inert in the deployment" gap every other
PrivacyCare seed script in this package (see seed_dsr.py,
seed_consent_demo.py) exists to close.

**parent_template_id inherits nothing.** It is a self-referential foreign
key with a relationship (AssessmentTemplate.parent_template in
models/privacy_assessment.py) and no code anywhere reads it — verified with
`grep -rn "parent_template_id\\|parent_template" src/ --include=*.py`,
which finds only its own Column/relationship declaration. So this script
does not rely on it for anything functional: it COPIES the 24 questions
from the GDPR DPIA template (GDPR_TEMPLATE_ID below — the 24-question GDPR
row, NOT the 23-question UK GDPR DPIA row) into a brand-new Kenya template,
and sets parent_template_id purely as provenance — a record of where the
questions came from, not a mechanism anything downstream depends on.

**Copied unedited.** Carol, the privacy SME, adapts the question text
afterwards; how much of the GDPR wording survives that pass is open
(OQ-W2-2). Writing bespoke Kenyan DPIA questions here would put
engineering's guesses into a compliance instrument — this script's only
job is to reproduce the GDPR template's questions byte-for-byte under a
new template row, with Kenyan region/authority/legal_reference metadata.

Same shape as scripts/privacycare/seed_dsr.py and seed_consent_demo.py:
one argparse CLI, one session, dry-run by default, --commit to persist.
_database_url() and _target_description() are copied verbatim from those
scripts (themselves copied from seed_connection.py, itself copied from
import_processes.py, itself copied from migrations/env.py) rather than
imported — see load_taxonomy.py's module docstring for why importing
env.py isn't safe here (it runs Alembic migrations as a side effect of the
import itself).

Secrets come from the environment at run time and are never written into
the repo or printed — main() only ever prints the target: line (host/port/
database, never the password, never the raw URL).

**No ORM writes, so no commit trap.** Unlike seed_dsr.py
(ensure_kenyan_policies' Policy/Rule.create_or_update) and
seed_consent_demo.py (PrivacyPreferenceHistory.create for the encrypted
email column), this script's two writes — the template row and the
question rows — are both plain columns Fides' own AssessmentTemplate/
AssessmentQuestion ORM classes never encrypt or otherwise need the ORM
for, so this seed is raw SQL through the session throughout (the brief's
own instruction: Fides' persist_obj commits unconditionally, so prefer raw
SQL). That means seed_kenya_template() never triggers persist_obj's
add/commit/refresh, and main() below needs no monkeypatch-commit-to-flush
guard around the seeding call the way seed_dsr.py's and
seed_consent_demo.py's own main()s do — there is no inner, unconditional
commit here for a dry run to accidentally survive. The real
db.commit()/db.rollback(), driven by args.commit, is the only commit
boundary in this script.

seed_kenya_template(db) itself never calls db.commit() or db.rollback() —
the caller's session boundary decides, same rule every other PrivacyCare
seed function follows. It is idempotent: an existing Kenya template
(looked up by assessment_type) is reused rather than re-created, and its
questions are only copied if none exist yet — so a second run, with or
without --commit, writes nothing new.
"""
import argparse
import os
import sys
from uuid import uuid4

import sqlalchemy
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# The GDPR Data Protection Impact Assessment template — 24 questions, the
# template this seed copies from.
#
# `SELECT id, name, region FROM assessment_template` against the live
# database returns 13 rows, not 3 — the table has held 13 for a while,
# unrelated to this task. Of those 13, two are easy to mistake for each
# other by name alone, and a third is a decoy if you try to disambiguate
# by region instead:
#   - ast_b5b6e569-0b77-439b-b1b8-8f9d77506f40, "GDPR Data Protection
#     Impact Assessment (DPIA)", region 'EU/EEA', 24 questions — this one.
#   - ast_fadf106a-8fd6-491e-8828-4ba509dcb85e, "UK GDPR Data Protection
#     Impact Assessment (DPIA)", region 'United Kingdom', 23 questions —
#     NOT this one; a different template with a near-identical name.
#   - ast_f8d6180a-b361-4819-b289-9cf46eb54a71, "UK ICO Record of
#     Processing Activities (ROPA)", ALSO region 'United Kingdom', 42
#     questions — not a DPIA at all; filtering by region alone would not
#     rule this one out.
# Selected ast_b5b6e569... because its name says "GDPR" rather than "UK
# GDPR", its type is DPIA rather than ROPA, and its question count (24)
# matches the count this seed is meant to reproduce.
GDPR_TEMPLATE_ID = "ast_b5b6e569-0b77-439b-b1b8-8f9d77506f40"

KENYA_ASSESSMENT_TYPE = "kenya_dpa_2019_dpia"
KENYA_TEMPLATE_VERSION = "KE-DPA-2019-DPIA-2021-01-14"
KENYA_TEMPLATE_NAME = "Kenya Data Protection Act, 2019 DPIA"
KENYA_REGION = "Kenya"
KENYA_AUTHORITY = "ODPC"
# Names both the primary statutory duty (DPA 2019 s.31 — the DPIA
# obligation itself) and the regulation that operationalises it, the same
# two-citation shape the brief asks for.
KENYA_LEGAL_REFERENCE = (
    "Data Protection Act, 2019 (No. 24 of 2019), section 31 (Data "
    "Protection Impact Assessment); Data Protection (General) "
    "Regulations, 2021."
)
KENYA_TEMPLATE_DESCRIPTION = (
    "Kenya Data Protection Act, 2019 DPIA questionnaire. Questions copied "
    "unedited from the GDPR DPIA template "
    f"({GDPR_TEMPLATE_ID}, see parent_template_id) as provenance only — "
    "parent_template_id inherits nothing at read time (see this script's "
    "own module docstring). Carol adapts the question text to Kenyan "
    "practice separately; this seed's only job is the copy."
)


def _database_url() -> str:
    # Copied from scripts/privacycare/seed_dsr.py's _database_url()
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


_FIND_GDPR_TEMPLATE_SQL = sqlalchemy.text(
    "SELECT id FROM assessment_template WHERE id = :id"
)
_FIND_KENYA_TEMPLATE_SQL = sqlalchemy.text(
    "SELECT id FROM assessment_template WHERE assessment_type = :assessment_type"
)
_INSERT_TEMPLATE_SQL = sqlalchemy.text(
    """
    INSERT INTO assessment_template
      (id, version, name, assessment_type, region, authority,
       legal_reference, description, is_active, fides_revision,
       is_managed, parent_template_id)
    VALUES (:id, :version, :name, :assessment_type, :region, :authority,
            :legal_reference, :description, true, 1, false,
            :parent_template_id)
    """
)
# is_managed=false: final whole-branch review, minor finding. is_managed is
# the one column on this table that names OWNERSHIP — whether Ethyca's own
# tooling considers itself responsible for a template's lifecycle. Nothing
# reads this column today, but true here would be a lie: this row is
# entirely PrivacyCare-authored (this script created it; this script is the
# only thing that will ever touch it again), not something Ethyca manages.
# false is the honest value, and the natural key any future Ethyca
# reconciliation job would use to leave this row alone.

_COUNT_QUESTIONS_SQL = sqlalchemy.text(
    "SELECT count(*) FROM assessment_question WHERE template_id = :template_id"
)
_COPY_QUESTIONS_SQL = sqlalchemy.text(
    """
    SELECT requirement_key, requirement_title, group_order, question_key,
           question_text, guidance, question_order, required,
           fides_sources, expected_coverage
    FROM assessment_question
    WHERE template_id = :template_id
    ORDER BY group_order, question_order
    """
)
_INSERT_QUESTION_SQL = sqlalchemy.text(
    """
    INSERT INTO assessment_question
      (id, template_id, requirement_key, requirement_title, group_order,
       question_key, question_text, guidance, question_order, required,
       fides_sources, expected_coverage)
    VALUES (:id, :template_id, :requirement_key, :requirement_title,
            :group_order, :question_key, :question_text, :guidance,
            :question_order, :required, :fides_sources, :expected_coverage)
    """
)


def _find_or_create_kenya_template(db: Session) -> tuple[str, bool]:
    """Returns (template_id, created)."""
    template_id = db.execute(
        _FIND_KENYA_TEMPLATE_SQL, {"assessment_type": KENYA_ASSESSMENT_TYPE}
    ).scalar()
    if template_id is not None:
        return template_id, False

    gdpr_exists = db.execute(
        _FIND_GDPR_TEMPLATE_SQL, {"id": GDPR_TEMPLATE_ID}
    ).scalar()
    if gdpr_exists is None:
        raise ValueError(
            f"GDPR DPIA template not found: {GDPR_TEMPLATE_ID!r} — cannot "
            "copy its questions into a new Kenya template"
        )

    template_id = f"ast_{uuid4()}"
    db.execute(
        _INSERT_TEMPLATE_SQL,
        {
            "id": template_id,
            "version": KENYA_TEMPLATE_VERSION,
            "name": KENYA_TEMPLATE_NAME,
            "assessment_type": KENYA_ASSESSMENT_TYPE,
            "region": KENYA_REGION,
            "authority": KENYA_AUTHORITY,
            "legal_reference": KENYA_LEGAL_REFERENCE,
            "description": KENYA_TEMPLATE_DESCRIPTION,
            # Provenance only — see this module's docstring. Nothing reads
            # this column at request time.
            "parent_template_id": GDPR_TEMPLATE_ID,
        },
    )
    return template_id, True


def _copy_questions(db: Session, template_id: str) -> int:
    """Copies the GDPR template's questions onto `template_id`, unedited,
    unless it already has questions (idempotency: a second run must not
    duplicate). Returns the number of rows actually inserted."""
    already = db.execute(
        _COUNT_QUESTIONS_SQL, {"template_id": template_id}
    ).scalar()
    if already:
        return 0

    rows = db.execute(
        _COPY_QUESTIONS_SQL, {"template_id": GDPR_TEMPLATE_ID}
    ).mappings().all()
    for row in rows:
        db.execute(
            _INSERT_QUESTION_SQL,
            {
                "id": f"asq_{uuid4()}",
                "template_id": template_id,
                "requirement_key": row["requirement_key"],
                "requirement_title": row["requirement_title"],
                "group_order": row["group_order"],
                "question_key": row["question_key"],
                # Copied unedited — see this module's docstring.
                "question_text": row["question_text"],
                "guidance": row["guidance"],
                "question_order": row["question_order"],
                "required": row["required"],
                "fides_sources": list(row["fides_sources"]),
                "expected_coverage": row["expected_coverage"],
            },
        )
    return len(rows)


def seed_kenya_template(db: Session) -> dict:
    """Find-or-create the Kenya DPA 2019 DPIA template, then copy the GDPR
    template's questions onto it if it does not already have its own.

    Never commits — the caller's session boundary decides, matching every
    other PrivacyCare seed function's own module docstring. Idempotent at
    both steps: a second call reuses whatever the first created and writes
    nothing new. Raises ValueError if the GDPR template this seed copies
    from cannot be found (only reachable on template creation — an
    already-existing Kenya template short-circuits before this is checked
    again).
    """
    template_id, template_created = _find_or_create_kenya_template(db)
    questions_copied = _copy_questions(db, template_id)
    return {
        "template_id": template_id,
        "template_created": template_created,
        "questions_copied": questions_copied,
    }


def _print_summary(summary: dict) -> None:
    print(f"template_id: {summary['template_id']}")
    print(f"template_created: {summary['template_created']}")
    print(f"questions_copied: {summary['questions_copied']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the Kenya Data Protection Act, 2019 DPIA "
        "template (assessment_type=kenya_dpa_2019_dpia) by copying the "
        "GDPR DPIA template's 24 questions unedited."
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
        # No monkeypatch-commit-to-flush guard here — see this module's own
        # docstring ("No ORM writes, so no commit trap"): seed_kenya_template
        # never calls an ORM .create(), so there is no unconditional inner
        # commit for a dry run to accidentally survive.
        try:
            summary = seed_kenya_template(db)
        except ValueError as exc:
            db.rollback()
            print(str(exc), file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            # A connection failure or a database error (e.g. an
            # IntegrityError) raised while seeding. NEVER print str(exc) or
            # the raw database_url: DBAPI driver error text can itself
            # embed the DSN, credentials included, on some drivers — the
            # same leak route seed_consent_demo.py's own handler guards
            # against. Only the exception's class name and the
            # already-safe target line's contents (host/port/db, never the
            # password) go to stderr.
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

        _print_summary(summary)

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
