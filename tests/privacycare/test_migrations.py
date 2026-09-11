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
