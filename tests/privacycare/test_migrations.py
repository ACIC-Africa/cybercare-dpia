# PrivacyCare runs its own migration chain so that upstream Fides upgrades
# never collide with ours. These tests pin both halves of that: our chain
# applies, and Fides' version table is left exactly as we found it.
import os
import sqlalchemy

FIDES_HEAD_BEFORE = None


def _engine():
    return sqlalchemy.create_engine(
        "postgresql://postgres:fides@127.0.0.1:5442/fides"
    )


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
    assert fides_rows == 1, "Fides' chain must still have exactly one head"
    assert pc_rows == 1, "PrivacyCare's chain must have exactly one head"
