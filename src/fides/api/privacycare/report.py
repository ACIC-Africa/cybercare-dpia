# The DPIA report, as a document MODEL — not a rendering.
#
# This is the artifact Josephine (a DPO) hands to Kenya's Office of the Data
# Protection Commissioner: under DPA 2019 s31 it is what proves an
# assessment happened before high-risk processing. This module produces the
# report's CONTENT as frozen dataclasses, so it is testable as data rather
# than as a string someone greps out of a PDF. No ReportLab, no HTML, no PDF
# — nothing here renders anything. Whatever eventually does (a future task)
# reads these dataclasses.
#
# Reuses the existing read path — _assessment_detail in api/assessments.py —
# rather than running a second, hand-rolled query against
# assessment_question/assessment_answer/answer_version. That function is
# already the single source of truth the admin-UI's detail screen renders
# from (questions, answers, statuses, evidence, answered_count,
# completeness). A report that disagrees with what the screen showed the DPO
# is worse than no report: the officer signs one artifact and the regulator
# reads a different one. answered_count/total_count/completeness below are
# summed straight off _assessment_detail's own question_groups/completeness
# — never recomputed here — so they cannot drift from it.
#
# The one thing _assessment_detail's validated response does NOT carry is
# per-question authorship: AssessmentQuestionResponse mirrors the admin-UI's
# TypeScript contract, which has no field for answer_version.created_by (the
# UI doesn't render it). _questions_for — the exact same helper
# _assessment_detail already calls internally for its own question rows —
# still has it on each raw row, so this module calls that one existing
# function a second time to recover it, rather than writing any new SQL.
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from fides.api.privacycare.api.assessments import _assessment_detail, _questions_for
from fides.api.privacycare.api.schemas import (
    AssessmentQuestionResponse,
    PrivacyAssessmentDetailResponse,
    QuestionGroup,
)


@dataclass(frozen=True)
class ReportCitation:
    number: int | None
    source_key: str | None
    value: str | None
    type: str | None  # "system" | "ai_analysis"


@dataclass(frozen=True)
class ReportQuestion:
    question_text: str
    answer_text: str
    answer_status: str  # complete | partial | needs_input
    answer_source: str  # system | ai_analysis | user_input | team_input
    author: str | None  # answer_version.created_by
    citations: list[ReportCitation]


@dataclass(frozen=True)
class ReportSection:
    title: str  # the requirement/group title
    questions: list[ReportQuestion]


@dataclass(frozen=True)
class Report:
    title: str
    metadata: list[
        tuple[str, str]
    ]  # label/value rows: system, data use, template, dates
    sections: list[ReportSection]
    answered_count: int
    total_count: int
    completeness: float
    export_mode: str


def _author_lookup(db: Session, assessment_id: str) -> dict[str, str | None]:
    """Map question_id -> answer_version.created_by (None if unanswered).

    Built from _questions_for's raw rows (fides.api.privacycare.api.
    assessments) — the same query _assessment_detail already runs
    internally, not a second one. See the module docstring for why this is
    the only field build_report needs that _assessment_detail's own
    response shape doesn't already expose.
    """
    lookup: dict[str, str | None] = {}
    for group in _questions_for(db, assessment_id):
        for q in group["questions"]:
            lookup[q["id"]] = q["answer_created_by"]
    return lookup


def _report_citation(item) -> ReportCitation:
    return ReportCitation(
        number=item.citation_number,
        source_key=item.source_key,
        value=item.value,
        type=item.type,
    )


def _report_question(
    q: AssessmentQuestionResponse, author: str | None
) -> ReportQuestion:
    return ReportQuestion(
        question_text=q.question_text,
        # Filed verbatim. Do NOT escape, sanitise or truncate answer_text
        # here — it is the officer's own words, and this module is a
        # document MODEL, not a renderer. Escaping (HTML, PDF, whatever)
        # belongs entirely to whatever eventually renders this data; doing
        # it here as well would either double-escape it or silently corrupt
        # the record a regulator is meant to be able to trust verbatim.
        answer_text=q.answer_text,
        answer_status=q.answer_status,
        answer_source=q.answer_source,
        author=author,
        citations=[_report_citation(item) for item in q.evidence],
    )


def _report_section(
    group: QuestionGroup, authors: dict[str, str | None]
) -> ReportSection:
    return ReportSection(
        title=group.title,
        questions=[
            _report_question(q, authors.get(q.question_id)) for q in group.questions
        ],
    )


def _metadata_rows(detail: PrivacyAssessmentDetailResponse) -> list[tuple[str, str]]:
    # "system, data use, template, dates" per the task brief, plus the
    # assessment's own name (explicitly required to appear in metadata) and
    # its status/risk so the artifact stands alone without the screen next
    # to it. A missing value renders as an empty string rather than
    # dropping the row entirely — the label itself ("Template", "Data Use",
    # ...) must always be present so a reviewer sees what wasn't captured,
    # not a document that's silently shorter than another.
    return [
        ("Assessment Name", detail.name or ""),
        ("System", detail.system_name or detail.system_fides_key or ""),
        ("Data Use", detail.data_use_name or detail.data_use or ""),
        ("Template", detail.template_name or ""),
        ("Status", detail.status or ""),
        ("Risk Level", detail.risk_level or ""),
        ("Created", detail.created_at or ""),
        ("Last Updated", detail.updated_at or ""),
    ]


def build_report(
    db: Session, assessment_id: str, *, export_mode: str = "external"
) -> Report:
    """Assemble a DPIA Report as data.

    Raises LookupError for an unknown assessment_id — propagated straight
    from _assessment_detail, which already raises it for exactly this case.
    """
    detail = _assessment_detail(db, assessment_id)
    authors = _author_lookup(db, assessment_id)

    sections = [_report_section(group, authors) for group in detail.question_groups]

    return Report(
        title=f"Data Protection Impact Assessment: {detail.name}",
        metadata=_metadata_rows(detail),
        sections=sections,
        # Summed directly off _assessment_detail's own per-group counts —
        # the exact numbers QuestionGroupPanel.tsx renders as "Fields:
        # {answeredCount}/{totalCount}" for each group on the screen — never
        # recomputed by a second query here. See the module docstring.
        answered_count=sum(g.answered_count for g in detail.question_groups),
        total_count=sum(g.total_count for g in detail.question_groups),
        completeness=detail.completeness or 0.0,
        export_mode=export_mode,
    )
