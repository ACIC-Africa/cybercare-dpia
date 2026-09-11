"""The assessment schema ships in OSS Fides; only its API is commercial.
This test pins that fact — if a Fides upgrade ever drops these tables,
W1 loses its foundation and we want to know immediately."""
import os
import sqlalchemy

EXPECTED_TABLES = {
    "assessment_template",
    "assessment_question",
    "privacy_assessment",
    "assessment_answer",
    "answer_version",
    "privacy_assessment_task",
}


def _engine():
    url = (
        f"postgresql://{os.environ.get('FIDES__DATABASE__USER', 'postgres')}:"
        f"{os.environ.get('FIDES__DATABASE__PASSWORD', 'fides')}@"
        f"{os.environ.get('FIDES__DATABASE__SERVER', '127.0.0.1')}:"
        f"{os.environ.get('FIDES__DATABASE__PORT', '5442')}/"
        f"{os.environ.get('FIDES__DATABASE__DB', 'fides')}"
    )
    return sqlalchemy.create_engine(url)


def test_assessment_tables_exist():
    inspector = sqlalchemy.inspect(_engine())
    present = set(inspector.get_table_names())
    missing = EXPECTED_TABLES - present
    assert not missing, f"assessment tables missing from the schema: {sorted(missing)}"


def test_assessment_template_carries_localisation_columns():
    """W2 derives a Kenyan template from the GDPR one via parent_template_id.
    These four columns are what make that possible."""
    inspector = sqlalchemy.inspect(_engine())
    cols = {c["name"] for c in inspector.get_columns("assessment_template")}
    for required in ("region", "authority", "legal_reference", "parent_template_id"):
        assert required in cols, f"assessment_template is missing {required}"
