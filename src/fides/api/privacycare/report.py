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
# reads a different one. answered_count/total_count are counted off the
# question rows that same function returned — never a second query here.
#
# WHAT THAT EQUALITY DOES AND DOES NOT BUY. An earlier revision of this
# docstring said the three numbers "cannot drift from" _assessment_detail.
# That is true of each number against its counterpart on the screen, and
# FALSE of the three numbers against EACH OTHER, because the detail
# response mixes two clocks: its per-group counts are counted live at read
# time, while its `completeness` is privacy_assessment.completeness, a
# column written at ANSWER time. Nothing refreshes that column when the
# template gains questions underneath a finished assessment — and Ethyca's
# own migrations add questions to an existing template in place — so
# reading it here produced sentences like "4 of 30 question(s) answered
# (16.7% complete)" (4/30 is 13.3%) and, worse, "24 of 30 question(s)
# answered (100.0% complete). 6 question(s) remain UNANSWERED". Inside one
# sentence of a DPA 2019 s31 filing, with the error in the direction that
# overstates compliance to the regulator.
#
# So this module does NOT read that column. `completeness` below is
# computed from answered_count/total_count — the same two numbers the
# document prints beside it — which is what makes the printed sentence
# incapable of contradicting itself whatever the column says. The column is
# left exactly as it is: other surfaces read it, and changing its semantics
# is a bigger question than one report.
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
from fides.api.privacycare.risk.odpc import OdpcFinding
from fides.api.privacycare.risk.odpc import evaluate as evaluate_odpc


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
    # 0-100. Computed from answered_count/total_count by _completeness —
    # never privacy_assessment.completeness. See the module docstring.
    completeness: float
    export_mode: str
    # Task 4 / spec D-W2-6: whether this DPIA must go to Kenya's ODPC before
    # processing begins, per risk.odpc.evaluate(). Carried both as this
    # typed field (for any caller that wants it as data, not text) and as a
    # metadata row (see _metadata_rows) — a DPIA that must be escalated
    # should not disclose that only to a caller that knows to look for it.
    odpc: OdpcFinding


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


def _odpc_metadata_value(finding: OdpcFinding) -> str:
    """The single metadata row's value: verdict, window and (when required)
    the driving risk, all in one place.

    Per the task brief: "The metadata rows are the natural home for the
    verdict and the window; the driving risk belongs with it." Built on
    finding.reason rather than re-deriving the verdict/band/window
    sentence a second time — that sentence already names both (see
    risk/odpc.py) — with the driving risk appended when there is one to
    name. A LOW/MEDIUM finding states "NOT required" explicitly (via
    reason) rather than the row being silently absent: silence reads as
    "not assessed", which is the wrong answer to a regulator's question.
    """
    # An explicit REQUIRED/NOT REQUIRED verdict leads the row: a DPO or
    # regulator scanning the metadata table for the one word that matters
    # must find it without reading the full sentence that follows — the
    # sentence (finding.reason) still carries the band and the window for
    # anyone who does read on.
    verdict = "REQUIRED" if finding.required else "NOT REQUIRED"
    value = f"{verdict} — {finding.reason}"
    if finding.required and finding.highest_risk is not None:
        risk = finding.highest_risk
        value += (
            f" Driving risk: {risk.category} — {risk.description} "
            f"(score {risk.score}/25, {risk.band})."
        )
    return value


def _metadata_rows(
    detail: PrivacyAssessmentDetailResponse, odpc: OdpcFinding
) -> list[tuple[str, str]]:
    # "system, data use, template, dates" per the task brief, plus the
    # assessment's own name (explicitly required to appear in metadata) and
    # its status/risk so the artifact stands alone without the screen next
    # to it. A missing value renders as an empty string rather than
    # dropping the row entirely — the label itself ("Template", "Data Use",
    # ...) must always be present so a reviewer sees what wasn't captured,
    # not a document that's silently shorter than another.
    #
    # "ODPC Prior Consultation" is deliberately placed among these rows,
    # not off in a footnote — see _odpc_metadata_value. Note this row's
    # verdict is Task 4's own computed band (risk.odpc.evaluate, driven by
    # risk.register.assessment_band) and can legitimately differ from the
    # "Risk Level" row above, which is Ethyca's own lossy three-value
    # projection (privacy_assessment.risk_level, collapsing CRITICAL into
    # "high"). That is not a bug to reconcile away: it is exactly the
    # distinction Task 4 exists to preserve.
    return [
        ("Assessment Name", detail.name or ""),
        ("System", detail.system_name or detail.system_fides_key or ""),
        ("Data Use", detail.data_use_name or detail.data_use or ""),
        ("Template", detail.template_name or ""),
        ("Status", detail.status or ""),
        ("Risk Level", detail.risk_level or ""),
        ("ODPC Prior Consultation", _odpc_metadata_value(odpc)),
        ("Created", detail.created_at or ""),
        ("Last Updated", detail.updated_at or ""),
    ]


def _is_answered(question: ReportQuestion) -> bool:
    """Does this question count as answered in the filed document?

    Two conditions, not one. `answer_status == "complete"` is the screen's
    own predicate (see _questions_for's answered_count) and excludes a
    machine draft, which is always `partial`. The second condition —
    non-blank text — is this module's: UpdateAnswerRequest.answer_text has
    no minimum length and write_answer defaults answer_status to
    "complete", so `PUT .../questions/{id}` with `{"answer_text": ""}`
    files a COMPLETE answer with nothing in it. That answer used to count
    toward the completion figure and print as a finished, attributed
    answer ("Author: carol@acme.io - Source: user_input - Status:
    COMPLETE") under a question with no answer beneath it. An answer with
    no text is not an answer, least of all to a regulator; this is the one
    place the report deliberately counts differently from the screen, and
    it counts DOWN — it can never make an assessment look more finished
    than the screen said.
    """
    return question.answer_status == "complete" and bool(
        (question.answer_text or "").strip()
    )


def _completeness(answered_count: int, total_count: int) -> float:
    """The percentage the document prints, derived from the two counts it
    prints beside it — 0-100, matching privacy_assessment.completeness's
    unit (not 0-1) and the `{:.1f}%` the renderer formats it with.

    A template with no questions is 0.0% complete, not 100%: an assessment
    nobody has been asked anything about has not been completed, and a
    ZeroDivisionError in a report build would surface to the officer as a
    503 on a document that is merely empty.
    """
    if total_count <= 0:
        return 0.0
    return (answered_count / total_count) * 100


def build_report(
    db: Session, assessment_id: str, *, export_mode: str = "external"
) -> Report:
    """Assemble a DPIA Report as data.

    Raises LookupError for an unknown assessment_id — propagated straight
    from _assessment_detail, which already raises it for exactly this case.
    """
    detail = _assessment_detail(db, assessment_id)
    authors = _author_lookup(db, assessment_id)
    # Task 4 / spec D-W2-6: computed fresh off the risk register on every
    # build, the same way answered_count/completeness are computed fresh
    # rather than read off a stored column — see the module docstring.
    odpc_finding = evaluate_odpc(db, assessment_id)

    sections = [_report_section(group, authors) for group in detail.question_groups]
    questions = [question for section in sections for question in section.questions]
    answered_count = sum(1 for question in questions if _is_answered(question))
    total_count = len(questions)

    return Report(
        title=f"Data Protection Impact Assessment: {detail.name}",
        metadata=_metadata_rows(detail, odpc_finding),
        sections=sections,
        # Counted off the questions THIS DOCUMENT PRINTS — the sections
        # above — not off a second query, and not off a number that could
        # describe a different set of questions than the ones on the page.
        # The predicate is _assessment_detail's own ("complete"), narrowed
        # by _is_answered to exclude a blank answer; see its docstring.
        # Equal to the screen's per-group "Fields: {answeredCount}/
        # {totalCount}" in every case except that blank one, which is
        # pinned by test_counts_match_the_detail_screen_exactly.
        answered_count=answered_count,
        total_count=total_count,
        # Derived from the two counts above, NOT read from
        # privacy_assessment.completeness. See the module docstring: the
        # stored column is written at answer time and is not refreshed when
        # a template gains questions, so printing it next to live counts is
        # how one sentence came to say "24 of 30 answered (100.0%
        # complete). 6 question(s) remain UNANSWERED". One number, one
        # source, inside one document.
        completeness=_completeness(answered_count, total_count),
        export_mode=export_mode,
        odpc=odpc_finding,
    )
