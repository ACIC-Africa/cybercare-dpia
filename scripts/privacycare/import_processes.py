#!/usr/bin/env python3
"""CLI wrapper around fides.api.privacycare.importers.processes.import_processes.

Same shape as scripts/privacycare/load_taxonomy.py: one argparse CLI, one
session, dry-run by default. `_database_url()` is copied from that script
(itself copied from migrations/env.py) verbatim, same precedence, same
reason it is not imported — see load_taxonomy.py's module docstring.

Arguments are deliberately narrow (D-IMP-2's plan scope: upsert only, no
delete): one positional `register` path, plus `--commit`. There is no
`--revert` here — deleting rows is not in this plan.

Validation of the register file (path exists, is valid JSON, is an object
with a `processes` list) happens in `_load_register()` BEFORE the database
session opens, so a malformed file never gets as far as taking a connection.
A `ValueError` raised by the core itself (a blank name/number, an ambiguous
external_ref) can only be discovered while the import runs, so it is caught
around the `import_processes()` call instead; either way the message is
printed to stderr with no traceback and the process exits 1.
"""
import argparse
import json
import os
import sys

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.importers.processes import ImportSummary, import_processes


def _database_url() -> str:
    # Copied from scripts/privacycare/load_taxonomy.py's _database_url()
    # verbatim (itself copied from src/fides/api/privacycare/migrations/
    # env.py's _database_url(), same precedence, same env vars, same
    # defaults) rather than imported — importing env.py runs Alembic
    # migrations as a side effect of the import itself, which would migrate
    # the database as a side effect of parsing this script's CLI args.
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


def _load_register(path: str) -> dict:
    """Read and validate the register file at `path`.

    Every failure mode raises ValueError with `path` named in the message,
    so the CLI can print it to stderr and exit 1 with no traceback: the file
    does not exist (or otherwise cannot be opened), the file is not valid
    JSON, or the parsed JSON is not an object with a `processes` list.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"{path}: {exc.strerror or exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from exc

    if not isinstance(data, dict) or not isinstance(data.get("processes"), list):
        raise ValueError(f"{path}: expected a JSON object with a 'processes' list")

    return data


def _print_summary(summary: ImportSummary) -> None:
    print(f"created: {summary.created}")
    print(f"updated: {summary.updated}")
    print(f"unchanged: {summary.unchanged}")
    print(f"without_data_mapping: {summary.without_data_mapping}")
    print(f"blank_applicability: {summary.blank_applicability}")
    # Largest count first, ties broken by name, so the biggest gaps in the
    # register are the first thing an operator's eye lands on.
    for name, count in sorted(summary.cycles.items(), key=lambda item: (-item[1], item[0])):
        print(f"cycle  {name}: {count}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a parsed business-process register (Task 1's "
        "import_processes) into privacycare_business_process."
    )
    parser.add_argument("register", help="Path to the register JSON file.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually commit the transaction. Without this, the run is a "
        "dry run (rolled back, nothing written).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    try:
        register = _load_register(args.register)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    engine = sqlalchemy.create_engine(_database_url())
    # Not `with Session(engine) as db:` — the sqlmypy plugin (still 1.x-era,
    # per pyproject.toml's [tool.mypy] plugins) doesn't see Session's
    # context-manager protocol and flags __enter__/__exit__ as missing.
    # try/finally gets the same close-on-exit guarantee without tripping it.
    db = Session(engine)
    try:
        try:
            summary = import_processes(db, register)
        except ValueError as exc:
            db.rollback()
            print(str(exc), file=sys.stderr)
            return 1

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
