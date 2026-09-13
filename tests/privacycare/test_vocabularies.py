# Every shared vocabulary, checked against the authorities that actually
# decide it — never against another copy of our own.
#
# Three independent audits converged on the same twelve lines: the frozensets
# in api/answers.py were pinned to NOTHING. Not to the live pg_enum, not to
# the shipped types.ts, not to Ethyca's own Python. Every mutation of them —
# adding a bogus value, removing a legal one — left the whole suite green.
# The consequences differ by direction and both are quiet:
#
#   a value we emit that the column rejects  -> DataError inside a Celery
#       worker, surfacing as an opaque task failure with the bad value buried
#       in a driver traceback;
#   a value we emit that the UI cannot map   -> a blank tag, silently;
#   a value we reject that is legal          -> a write refused for no reason;
#   a value Ethyca's ORM cannot read back    -> LookupError on THEIR code
#       reading a row WE wrote.
#
# So each vocabulary is checked three ways where three authorities exist:
# the live database (what can be stored), the shipped TypeScript (what can be
# rendered), and Ethyca's Python (what upstream can read back). A vocabulary
# that satisfies all three cannot drift silently in any direction.
import pathlib
import re

import pytest
import sqlalchemy

from fides.api.models import privacy_assessment as ethyca
from fides.api.models.worker_task import ExecutionLogStatus
from fides.api.privacycare.api.answers import (
    _ANSWER_SOURCES,
    _ANSWER_STATUSES,
    _CHANGE_TYPES,
)
from fides.api.privacycare.api.schemas import AssessmentStatus, RiskLevel
from fides.api.privacycare.context import UNSUPPORTED_SOURCE_ROOTS
from fides.api.privacycare.tasks import TASK_ACTION_TYPE, TASK_STATUSES_WRITTEN
from tests.privacycare.test_answers import DB_URL

TYPES_TS = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/features/privacy-assessments/types.ts"
)


def _pg_labels(typname: str) -> set[str]:
    """The labels Postgres will actually accept for this enum type."""
    engine = sqlalchemy.create_engine(DB_URL)
    with engine.connect() as connection:
        labels = {
            row[0]
            for row in connection.execute(
                sqlalchemy.text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :n"
                ),
                {"n": typname},
            )
        }
    assert labels, f"no pg_enum type named {typname!r} — the schema moved"
    return labels


def _ts_enum(name: str) -> set[str]:
    """The values the shipped admin UI can render for this enum."""
    text = TYPES_TS.read_text(encoding="utf-8")
    match = re.search(rf"export enum {name} \{{(.*?)\}}", text, re.S)
    assert match, f"no `export enum {name}` in {TYPES_TS} — the contract moved"
    values = set(re.findall(r'=\s*"([^"]+)"', match.group(1)))
    assert values, f"parsed no values out of {name}"
    return values


def _py_enum(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


# (our set, pg_enum type, types.ts enum, Ethyca's Python enum)
EQUAL_THREE_WAYS = [
    ("answer_status", _ANSWER_STATUSES, "answerstatus", "AnswerStatus", ethyca.AnswerStatus),
    ("answer_source", _ANSWER_SOURCES, "answersource", "AnswerSource", ethyca.AnswerSource),
    (
        "assessment_status",
        set(_py_enum(AssessmentStatus)),
        "assessmentstatus",
        "AssessmentStatus",
        ethyca.AssessmentStatus,
    ),
    ("risk_level", set(_py_enum(RiskLevel)), "risklevel", "RiskLevel", ethyca.RiskLevel),
]


@pytest.mark.parametrize(
    "label,ours,pg_type,ts_name,ethyca_enum",
    EQUAL_THREE_WAYS,
    ids=[row[0] for row in EQUAL_THREE_WAYS],
)
def test_vocabulary_agrees_with_database_ui_and_upstream(
    label, ours, pg_type, ts_name, ethyca_enum
):
    ours = set(ours)
    assert ours == _pg_labels(pg_type), (
        f"{label}: our values disagree with what Postgres accepts. A value we "
        f"emit but the column rejects becomes an opaque DataError inside a "
        f"Celery worker; a value we reject but the column accepts is a write "
        f"refused for no reason."
    )
    assert ours == _ts_enum(ts_name), (
        f"{label}: our values disagree with the shipped admin UI. A value the "
        f"UI cannot map renders as a blank tag, with no error anywhere."
    )
    assert ours == _py_enum(ethyca_enum), (
        f"{label}: our values disagree with Ethyca's own enum. Their ORM "
        f"raises LookupError reading a row we wrote, and the next upstream "
        f"merge silently widens or narrows the vocabulary under us."
    )


def test_change_type_agrees_with_database_and_upstream():
    # No types.ts counterpart: answer_version.change_type drives no UI control
    # (QuestionCard renders answer_source, not change_type), so there are only
    # two authorities here rather than three. Stated rather than silently
    # skipped — an absent authority is a fact about the contract.
    ours = set(_CHANGE_TYPES)
    assert ours == _pg_labels("answerchangetype")
    assert ours == _py_enum(ethyca.AnswerChangeType)


def test_the_task_statuses_we_write_are_readable_by_both_consumers():
    # privacy_assessment_task.status is a plain varchar — Postgres will accept
    # ANY string, so the database is not an authority here and cannot catch a
    # typo. The two real authorities are Ethyca's ORM (which must read the row
    # back) and the UI (which must render it).
    upstream = _py_enum(ExecutionLogStatus)
    assert TASK_STATUSES_WRITTEN <= upstream, (
        "a status Ethyca's ExecutionLogStatus does not know makes THEIR code "
        f"raise LookupError on a row WE wrote: {TASK_STATUSES_WRITTEN - upstream}"
    )
    renderable = _ts_enum("TaskStatus")
    assert TASK_STATUSES_WRITTEN <= renderable, (
        "a status the UI cannot render leaves the progress indicator blank: "
        f"{TASK_STATUSES_WRITTEN - renderable}"
    )


def test_the_action_type_we_write_is_one_upstream_defines():
    assert TASK_ACTION_TYPE in _py_enum(ethyca.AssessmentTaskType)


def test_expected_coverage_vocabulary_matches_the_shipped_questions():
    # expected_coverage is a plain varchar with no check constraint, so the
    # only authority is the data itself. The coverage policy branches on these
    # exact strings; a fourth level added upstream would fall through every
    # branch and silently generate nothing for those questions.
    from fides.api.privacycare.generator import HANDLED_COVERAGE_LEVELS

    engine = sqlalchemy.create_engine(DB_URL)
    with engine.connect() as connection:
        live = {
            row[0]
            for row in connection.execute(
                sqlalchemy.text("SELECT DISTINCT expected_coverage FROM assessment_question")
            )
        }
    assert live, "no questions in the database — this test is measuring nothing"
    assert live == HANDLED_COVERAGE_LEVELS, (
        "the coverage policy does not handle every expected_coverage value the "
        f"shipped templates use. Unhandled: {live - HANDLED_COVERAGE_LEVELS}; "
        f"handled but absent from the data: {HANDLED_COVERAGE_LEVELS - live}"
    )


def test_questionnaire_statuses_agree_with_database_ui_and_upstream():
    # questionnaire.status IS a native Postgres enum (verified against the
    # live schema: information_schema.columns reports udt_name
    # 'questionnairestatus', a USER-DEFINED type — unlike
    # privacy_assessment_task.status, which is a plain varchar). All three
    # authorities apply, same as EQUAL_THREE_WAYS above.
    from fides.api.models.questionnaire import QuestionnaireStatus
    from fides.api.privacycare.chat import QUESTIONNAIRE_STATUSES

    ours = set(QUESTIONNAIRE_STATUSES)
    assert ours == _pg_labels("questionnairestatus")
    assert ours == _ts_enum("QuestionnaireSessionStatus")
    assert ours == _py_enum(QuestionnaireStatus)


def test_every_fides_sources_root_is_either_supplied_or_recorded_as_unsupported():
    # Derived from the live templates, not from our own frozenset — the
    # previous version of this check restated UNSUPPORTED_SOURCE_ROOTS back to
    # itself, so a tenth root added upstream would have fallen through both the
    # context builder and the "recorded gap" list with nothing to notice.
    from fides.api.privacycare.context import SUPPLIED_SOURCE_ROOTS

    engine = sqlalchemy.create_engine(DB_URL)
    with engine.connect() as connection:
        live = {
            row[0]
            for row in connection.execute(
                sqlalchemy.text(
                    "SELECT DISTINCT split_part(src, '.', 1) "
                    "FROM assessment_question q, unnest(q.fides_sources) AS src"
                )
            )
        }
    assert live, "no fides_sources in the database — this test is measuring nothing"
    accounted = SUPPLIED_SOURCE_ROOTS | UNSUPPORTED_SOURCE_ROOTS
    assert live <= accounted, (
        "a fides_sources root is neither supplied by build_context nor recorded "
        f"in UNSUPPORTED_SOURCE_ROOTS: {live - accounted}. Unaccounted roots "
        "resolve to None and their questions go unanswered, with nothing saying why."
    )
    assert accounted <= live, (
        f"we account for roots the shipped templates never cite: {accounted - live}"
    )
