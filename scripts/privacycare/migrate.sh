#!/bin/bash
# Runs PrivacyCare's own Alembic chain (privacycare_alembic.ini) up to head.
#
# I3: this file existed but was wired to no entrypoint, Dockerfile, or CI
# job. Fides migrates itself at container/app startup; PrivacyCare does
# not piggyback on that (deliberately — see privacycare_alembic.ini /
# src/fides/api/privacycare/migrations/env.py: a separate version table,
# a separate MetaData, so our chain can never tangle with or autogenerate
# a drop of a Fides table). Without something invoking this script, a
# fresh deploy ends up with every Fides table and zero PrivacyCare tables.
#
# MUST run AFTER Fides' own startup migrations have completed. This script
# does not create the `fides` database itself, nor does it wait for
# Fides to become healthy — it assumes Fides (or an operator) already got
# the database to the point where `ctl_systems` / `privacydeclaration`
# exist, since PrivacyCare's own tables reference Fides data by id (see
# src/fides/api/privacycare/ropa.py) without owning it.
#
# Why separate rather than folded into Fides' own migration step:
# PrivacyCare keeps its own chain so it can never tangle with or
# autogenerate a drop of a Fides table (the "never edit Ethyca files" rule
# was retired 2026-09-13 — see
# docs/superpowers/specs/2026-09-13-privacycare-kenyan-taxonomy-design.md;
# separation here is about safety, not permission). This script is the
# operational seam that makes that possible: run it as its own step,
# right after Fides' migrations, in whatever deploy/entrypoint sequence
# stands up the app.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# Database connection: same env vars and defaults as
# src/fides/api/privacycare/migrations/env.py's _database_url(), so this
# script and the chain it drives always agree on where "the database" is.
export FIDES__DATABASE__SERVER="${FIDES__DATABASE__SERVER:-127.0.0.1}"
export FIDES__DATABASE__USER="${FIDES__DATABASE__USER:-postgres}"
export FIDES__DATABASE__PASSWORD="${FIDES__DATABASE__PASSWORD:-fides}"
export FIDES__DATABASE__PORT="${FIDES__DATABASE__PORT:-5442}"
export FIDES__DATABASE__DB="${FIDES__DATABASE__DB:-fides}"

echo "[privacycare/migrate.sh] target: ${FIDES__DATABASE__USER}@${FIDES__DATABASE__SERVER}:${FIDES__DATABASE__PORT}/${FIDES__DATABASE__DB}"

# I4: `fides db reset` drops Fides' own tables and Fides' own
# alembic_version, but PrivacyCare's tables survive that reset — holding
# links to privacydeclaration ids that no longer exist. Every ROPA then
# reads as 100% missing_declarations, while PrivacyCare's own chain still
# reports head (its tables and its version table were never touched), so
# nothing here would otherwise re-run or flag it. Detect that exact
# scenario before migrating and warn loudly; never auto-delete data.
uv run --python 3.13 python3 - <<'PYEOF'
import os
import sys

import sqlalchemy

url = "postgresql://{u}:{p}@{h}:{port}/{db}".format(
    u=os.environ["FIDES__DATABASE__USER"],
    p=os.environ["FIDES__DATABASE__PASSWORD"],
    h=os.environ["FIDES__DATABASE__SERVER"],
    port=os.environ["FIDES__DATABASE__PORT"],
    db=os.environ["FIDES__DATABASE__DB"],
)

engine = sqlalchemy.create_engine(url)
with engine.connect() as conn:
    inspector = sqlalchemy.inspect(conn)
    table_names = set(inspector.get_table_names())

    privacycare_row_count = 0
    if "privacycare_business_process" in table_names:
        privacycare_row_count += conn.execute(
            sqlalchemy.text("SELECT count(*) FROM privacycare_business_process")
        ).scalar_one()
    if "privacycare_process_declaration" in table_names:
        privacycare_row_count += conn.execute(
            sqlalchemy.text("SELECT count(*) FROM privacycare_process_declaration")
        ).scalar_one()

    fides_alembic_version_present_and_populated = False
    if "alembic_version" in table_names:
        fides_version_rows = conn.execute(
            sqlalchemy.text("SELECT count(*) FROM alembic_version")
        ).scalar_one()
        fides_alembic_version_present_and_populated = fides_version_rows > 0

    if privacycare_row_count > 0 and not fides_alembic_version_present_and_populated:
        print("", file=sys.stderr)
        print("!" * 78, file=sys.stderr)
        print("! WARNING: ORPHANED PRIVACYCARE DATA DETECTED", file=sys.stderr)
        print("!", file=sys.stderr)
        print(
            "! PrivacyCare's own tables hold rows, but Fides' `alembic_version`",
            file=sys.stderr,
        )
        print(
            "! table is empty or absent. This is the exact signature of a",
            file=sys.stderr,
        )
        print(
            "! `fides db reset`: it drops Fides' tables and Fides' own",
            file=sys.stderr,
        )
        print(
            "! alembic_version, but PrivacyCare's tables and its separate",
            file=sys.stderr,
        )
        print(
            "! privacycare_alembic_version survive untouched.",
            file=sys.stderr,
        )
        print("!", file=sys.stderr)
        print(
            "! PrivacyCare's business_process / process_declaration rows now",
            file=sys.stderr,
        )
        print(
            "! point at privacydeclaration ids that no longer exist. Every",
            file=sys.stderr,
        )
        print(
            "! ROPA will read as 100% missing_declarations until this is",
            file=sys.stderr,
        )
        print(
            "! addressed. PrivacyCare's own migration chain will still report",
            file=sys.stderr,
        )
        print(
            "! itself at head, so nothing here re-runs or fixes this for you.",
            file=sys.stderr,
        )
        print("!", file=sys.stderr)
        print(
            "! This script will NOT delete anything. An operator must decide",
            file=sys.stderr,
        )
        print(
            "! whether to re-link, re-seed, or manually clear the orphaned",
            file=sys.stderr,
        )
        print("! PrivacyCare rows.", file=sys.stderr)
        print("!" * 78, file=sys.stderr)
        print("", file=sys.stderr)
PYEOF

echo "[privacycare/migrate.sh] running: alembic -c privacycare_alembic.ini upgrade head"
exec uv run --python 3.13 alembic -c privacycare_alembic.ini upgrade head
