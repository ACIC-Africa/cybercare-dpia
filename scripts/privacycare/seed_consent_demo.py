#!/usr/bin/env python3
"""Seed a DEMONSTRATION notice so the stale-consent detector
(fides.api.privacycare.consent.detector.find_stale_consents) has something
true to find.

Plan 16, Task 4. The consent tables (privacynotice, noticetranslation,
privacynoticehistory, privacypreferencehistory) hold zero rows in the live
database, and privacycare_consent_rule — the materiality-rule configuration
table Task 1 introduced — is never populated by its own migration (see
that migration's own docstring: "This migration does NOT insert a default
row. Populating the row is Task 4's seed CLI's job"). Without this script,
Tasks 1-3 are exactly the workstream's recurring failure: a capability that
exists in the schema and is inert in the deployment.

**This notice is NOT real customer content.** It is keyed
`privacycare_demo_fuel_card_marketing` and named so nobody could mistake it
for something Josephine's business actually shows a data subject. It must
never be extended into her real consent notices — those are hers and
Carol's to author (OQ-CON-01/OQ-CON-03); fabricating them here would put
false statements about her processing into a compliance record. What is
seeded is exactly enough for the detector to demonstrate one true finding:
one notice with two history versions (v1 -> v2 gains
`marketing.advertising.third_party`), and one affirmative `opt_in`
preference recorded against v1 for a synthetic subject
(DEMO_SUBJECT_EMAIL, an `@example.invalid` address — RFC 2606 reserves
`.invalid` for addresses that are not, and can never become, real).

**It seeds the rule row first.** `find_stale_consents` calls `active_rule`
(fides.api.privacycare.consent.materiality), which raises on an empty
table. `seed_consent_rule` (Task 1) is idempotent — a fixed row id under
`ON CONFLICT DO NOTHING` — so calling it here, ahead of the notice, is safe
regardless of whether this script has already run.

Same shape as scripts/privacycare/seed_dsr.py: one argparse CLI, one
session, dry-run by default, `--commit` to persist. `_database_url()` and
`_target_description()` are copied verbatim from seed_dsr.py (itself copied
from seed_connection.py, itself copied from import_processes.py, itself
copied from migrations/env.py) rather than imported — see
load_taxonomy.py's module docstring for why importing env.py isn't safe
here (it runs Alembic migrations as a side effect of the import itself).

Secrets come from the environment at run time and are never written into
the repo or printed — `main()` only ever prints the `target:` line
(host/port/database, never the password, never the raw URL).

**The encryption trap (cost Task 2 a fix round).** `privacypreferencehistory
.email` / `.fides_user_device` / `.external_id` are `StringEncryptedType`
(AES-GCM) columns — see `fides.api.models.privacy_preference.
ConsentIdentitiesMixin` and detector.py's own module docstring. A raw SQL
INSERT into `email` writes plaintext where the ORM's decrypting type
decorator expects ciphertext, and the detector's ORM-based identity lookup
(`find_stale_consents` loads matched preference rows through the
`PrivacyPreferenceHistory` model specifically to get that decryption) would
then fail to read it back. So the notice, its translation and its two
history versions are plain raw-SQL INSERTs (none of those columns are
encrypted), but the one preference row goes through
`PrivacyPreferenceHistory.create` — the ORM path that actually encrypts
`email` at rest.

**The commit trap.** Fides' `OrmWrappedFidesBase.persist_obj`
(fides.api.db.base_class) does `add`/`commit`/`refresh` UNCONDITIONALLY as
part of every ORM create — not optional, and `PrivacyPreferenceHistory.
create` has no way to suppress it. Calling it against a real, unpatched
session would end the transaction outright the moment this script's own
seeding call returns, making even a plain "dry run" (no --commit) a real
permanent write. `main()` carries the exact guard seed_dsr.py's own
docstring documents and every test fixture in this package already uses:
`session.commit` is monkeypatched to `session.flush` for the duration of
the seeding call only, restored immediately after (success or failure
alike), so the REAL `db.commit()`/`db.rollback()` below — driven by
`args.commit` — is what actually decides whether anything survives.

`seed_consent_demo(db)` itself never calls `db.commit()` or
`db.rollback()` — the caller's session boundary decides, same rule
seed_dsr.py's own seed_dsr() follows. It is idempotent: a notice already
present by key is reused rather than re-created (checked by key, then by
version, then by whether a preference already exists against v1), so a
second run of this script -- with or without --commit -- writes nothing
new.
"""
import argparse
import os
import sys
from uuid import uuid4

import sqlalchemy
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import fides.api.db.base  # noqa: F401 — see below
from fides.api.models.privacy_preference import PrivacyPreferenceHistory
from fides.api.privacycare.consent.materiality import seed_consent_rule

# `fides.api.db.base` is imported (but not from) purely for its side
# effect: it is Alembic's single central import point that pulls in every
# mapped SQLAlchemy class before any of them are used. PrivacyPreference
# History.create touches relationships (e.g. to PrivacyRequest) directly
# through the ORM, and without this import SQLAlchemy can fail to resolve
# them the first time the mapper configures — the exact failure mode
# seed_connection.py's own docstring documents for ConnectionConfig.

# A demonstration notice, not a real one -- named so nobody mistakes it for
# something Josephine's business actually shows a data subject.
NOTICE_KEY = "privacycare_demo_fuel_card_marketing"
NOTICE_NAME = (
    "PrivacyCare DEMO Notice — Fuel Card Marketing "
    "(synthetic seed data, not a real customer notice)"
)
V1_DATA_USES = ["marketing.advertising"]
V2_DATA_USES = ["marketing.advertising", "marketing.advertising.third_party"]
ADDED_USE = "marketing.advertising.third_party"

# A synthetic subject. `.invalid` is reserved by RFC 2606 for addresses
# that are not, and can never become, real -- the identity equivalent of
# NOTICE_KEY's "DEMO" marker.
DEMO_SUBJECT_EMAIL = "privacycare-demo-subject@example.invalid"


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


_FIND_NOTICE_SQL = sqlalchemy.text(
    "SELECT id FROM privacynotice WHERE notice_key = :key"
)
_FIND_TRANSLATION_SQL = sqlalchemy.text(
    "SELECT id FROM noticetranslation "
    "WHERE privacy_notice_id = :notice_id AND language = 'en'"
)
_FIND_VERSION_SQL = sqlalchemy.text(
    "SELECT id FROM privacynoticehistory "
    "WHERE translation_id = :translation_id AND version = :version"
)
_COUNT_PREFERENCE_SQL = sqlalchemy.text(
    "SELECT count(*) FROM privacypreferencehistory "
    "WHERE privacy_notice_history_id = :history_id"
)

_INSERT_NOTICE_SQL = sqlalchemy.text(
    """
    INSERT INTO privacynotice
      (id, name, notice_key, consent_mechanism, disabled,
       enforcement_level, has_gpc_flag, data_uses)
    VALUES (:id, :name, :key, 'opt_in', false, 'system_wide', false, :uses)
    """
)
_INSERT_TRANSLATION_SQL = sqlalchemy.text(
    """
    INSERT INTO noticetranslation (id, language, privacy_notice_id, title)
    VALUES (:id, 'en', :notice_id, :title)
    """
)
_INSERT_VERSION_SQL = sqlalchemy.text(
    """
    INSERT INTO privacynoticehistory
      (id, name, notice_key, title, version, data_uses, translation_id,
       consent_mechanism, disabled, enforcement_level, has_gpc_flag)
    VALUES (:id, :name, :key, :name, :version, :uses, :translation_id,
            'opt_in', false, 'system_wide', false)
    """
)


def _find_or_create_notice(db: Session) -> tuple[str, str, bool]:
    """Returns (notice_id, translation_id, created).

    Final review, minor: the English-translation lookup can legitimately
    come back empty for a notice that DOES exist — `_FIND_TRANSLATION_SQL`
    matches `language = 'en'` only, and Ethyca's own
    `delete_notice_translations` (called by `PrivacyNotice.update` for every
    translation not supplied in an update request) will happily remove it.
    Returning that `None` unchecked made `_FIND_VERSION_SQL` match nothing
    (`translation_id = NULL` is never true) and the inserts below produce
    orphaned, `translation_id`-NULL history rows on every rerun, needing a
    manual DB fix. The English translation is recreated instead: this is a
    demonstration notice this script owns end to end, so restoring the
    piece it needs is the idempotent answer, not a failure.
    """
    notice_id = db.execute(_FIND_NOTICE_SQL, {"key": NOTICE_KEY}).scalar()
    if notice_id is not None:
        translation_id = db.execute(
            _FIND_TRANSLATION_SQL, {"notice_id": notice_id}
        ).scalar()
        if translation_id is None:
            translation_id = str(uuid4())
            db.execute(
                _INSERT_TRANSLATION_SQL,
                {
                    "id": translation_id,
                    "notice_id": notice_id,
                    "title": NOTICE_NAME,
                },
            )
        return notice_id, translation_id, False

    notice_id = str(uuid4())
    translation_id = str(uuid4())
    db.execute(
        _INSERT_NOTICE_SQL,
        {
            "id": notice_id,
            "name": NOTICE_NAME,
            "key": NOTICE_KEY,
            # The live notice row's own data_uses mirror the live (v2)
            # history version -- history is the source of truth the
            # detector reads, but leaving this at v1's uses would make the
            # notice's own row lie about what it currently asks for.
            "uses": V2_DATA_USES,
        },
    )
    db.execute(
        _INSERT_TRANSLATION_SQL,
        {"id": translation_id, "notice_id": notice_id, "title": NOTICE_NAME},
    )
    return notice_id, translation_id, True


def _find_or_create_version(
    db: Session, *, translation_id: str, version: float, data_uses: list[str]
) -> tuple[str, bool]:
    """Returns (history_id, created)."""
    history_id = db.execute(
        _FIND_VERSION_SQL, {"translation_id": translation_id, "version": version}
    ).scalar()
    if history_id is not None:
        return history_id, False

    history_id = str(uuid4())
    db.execute(
        _INSERT_VERSION_SQL,
        {
            "id": history_id,
            "name": NOTICE_NAME,
            "key": NOTICE_KEY,
            "version": version,
            "uses": data_uses,
            "translation_id": translation_id,
        },
    )
    return history_id, True


def seed_consent_demo(db: Session) -> dict:
    """Seed the active materiality rule, then the demo notice, its two
    history versions and its one affirmative preference — in that order,
    because `find_stale_consents` calls `active_rule`, which raises on an
    empty `privacycare_consent_rule` table (see the module docstring).

    Never commits — the caller's session boundary decides, matching
    seed_dsr.py's own seed_dsr(). Idempotent at every step: a second call
    reuses whatever this call already created and writes nothing new.
    Returns a summary dict describing what was found vs. created.
    """
    seed_consent_rule(db)

    notice_id, translation_id, notice_created = _find_or_create_notice(db)
    v1_id, v1_created = _find_or_create_version(
        db, translation_id=translation_id, version=1.0, data_uses=V1_DATA_USES
    )
    v2_id, v2_created = _find_or_create_version(
        db, translation_id=translation_id, version=2.0, data_uses=V2_DATA_USES
    )

    existing_preferences = db.execute(
        _COUNT_PREFERENCE_SQL, {"history_id": v1_id}
    ).scalar()
    if existing_preferences:
        preference_id = None
        preference_created = False
    else:
        # ORM path deliberately -- see the module docstring's "encryption
        # trap": `email` is a StringEncryptedType column, and only the ORM's
        # create/persist_obj path actually encrypts it at rest. check_name
        # =False because PrivacyPreferenceHistory carries no `.name`
        # attribute for OrmWrappedFidesBase.create's optional uniqueness
        # check to inspect.
        preference = PrivacyPreferenceHistory.create(
            db,
            data={
                "preference": "opt_in",
                "privacy_notice_history_id": v1_id,
                "email": DEMO_SUBJECT_EMAIL,
            },
            check_name=False,
        )
        preference_id = preference.id
        preference_created = True

    return {
        "notice_id": notice_id,
        "notice_created": notice_created,
        "translation_id": translation_id,
        "v1_id": v1_id,
        "v1_created": v1_created,
        "v2_id": v2_id,
        "v2_created": v2_created,
        "preference_id": preference_id,
        "preference_created": preference_created,
    }


def _print_summary(summary: dict) -> None:
    print(f"notice_key: {NOTICE_KEY}")
    print(f"notice_created: {summary['notice_created']}")
    print(f"v1_created: {summary['v1_created']}  v2_created: {summary['v2_created']}")
    print(f"preference_created: {summary['preference_created']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed a demonstration notice (privacycare_demo_fuel_"
        "card_marketing) with two history versions and one stale opt-in "
        "preference, plus the consent-materiality rule row, so the "
        "stale-consent detector has something true to find."
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
        # See seed_consent_demo()'s and the module's own docstring for why
        # this is here at all: PrivacyPreferenceHistory.create's write goes
        # through Fides' own ORM, whose persist_obj calls db.commit()
        # UNCONDITIONALLY — with no patch, that inner commit would end the
        # real transaction the moment seed_consent_demo returns, making
        # everything below (including a plain "dry run" with no --commit)
        # a real, permanent write regardless of args.commit. Absorbing
        # db.commit() into db.flush() for the duration of the seeding call
        # is the exact pattern every test fixture in this package already
        # relies on (`monkeypatch.setattr(session, "commit",
        # session.flush)`) — restored immediately after (success or
        # failure alike), so the REAL db.commit()/db.rollback() below is
        # what actually decides whether anything survives.
        real_commit = db.commit
        db.commit = db.flush  # type: ignore[method-assign]
        try:
            summary = seed_consent_demo(db)
        except ValueError as exc:
            db.commit = real_commit  # type: ignore[method-assign]
            db.rollback()
            print(str(exc), file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            # Fix round 1, Finding 2: a connection failure (server
            # unreachable, auth refused) or a database error (e.g. an
            # IntegrityError) raised while seeding. Inherited from
            # seed_dsr.py's identical gap — that script's own fix is a
            # separate plan's business, noted in the fix report, not made
            # here. NEVER print str(exc) or the raw database_url: DBAPI
            # driver error text can itself embed the DSN, credentials
            # included, on some drivers, and a bare traceback is exactly
            # the leak route the brief forbids. Only the exception's own
            # class name and the already-safe `target` line's contents
            # (host/port/db, never the password) go to stderr.
            db.commit = real_commit  # type: ignore[method-assign]
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
        db.commit = real_commit  # type: ignore[method-assign]

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
