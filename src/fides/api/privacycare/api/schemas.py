# Response schemas for the DPIA read surface.
#
# Field names and optionality are copied from the generated TypeScript types in
# clients/admin-ui/src/types/api/models/. The shipped admin-UI is compiled
# against those types, so this file does not get to choose its own shape.
import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Fix round 3 (whole-range review, MAJOR finding): the two enums that the
# request contract below is typed against.
#
# `status` and `risk_level` used to be bare `Optional[str]` on
# UpdatePrivacyAssessmentRequest. Both underlying columns are NATIVE
# POSTGRES ENUMS (assessmentstatus / risklevel), and _update_assessment
# writes them through raw sqlalchemy.text(), which bypasses SQLAlchemy's own
# EnumColumn validation exactly the way that function's docstring already
# documents for `onupdate`. So `{"status": "archived"}` reached Postgres and
# raised DataError (InvalidTextRepresentation) — uncaught, since
# update_assessment catches only LookupError and the app registers no
# SQLAlchemyError handler — and surfaced to a DPO's client as an opaque 500.
#
# That was the odd one out on this surface's error-mapping axis: every other
# client error here is a 404 or a 422, and
# _reject_explicit_null_for_not_null_columns below exists for precisely this
# class of problem ("rather than let that surface as a raw Postgres
# IntegrityError bubbling out of the route as a 500"). It covered explicit
# NULL and stopped there; the invalid-label case is the same class and was
# left to bubble.
#
# The members below are copied from the SHIPPED TypeScript enums of the same
# names in clients/admin-ui/src/features/privacy-assessments/types.ts, which
# UpdatePrivacyAssessmentRequest's TS counterpart types its two fields
# against. They are not restated from memory and must not drift:
# test_assessment_status_enum_matches_the_shipped_contract /
# test_risk_level_enum_matches_the_shipped_contract (test_api_schemas.py)
# parse that file and assert member-for-member equality, so a UI-side
# addition fails here rather than 500-ing in production. Both also match the
# live Postgres enum labels exactly (verified: assessmentstatus =
# in_progress, completed, outdated, generating; risklevel = high, medium,
# low).
#
# `str, Enum` (not bare Enum) so these compare and serialise as their string
# values everywhere the rest of this module already treats them as strings.
class AssessmentStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OUTDATED = "outdated"
    GENERATING = "generating"


class RiskLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AssessmentResponse(BaseModel):
    id: str
    template_id: str
    template_name: Optional[str] = None
    name: str
    status: str
    completeness: Optional[float] = 0.0
    risk_level: Optional[str] = None
    # Fix wave (Screen 2 review), finding 1. risk_level immediately above is
    # Ethyca's own lossy three-value projection (risk/banding.py's
    # projected_risk_level writes CRITICAL there as "high") — the assessment
    # LIST screen (AssessmentCard.tsx) used to render THAT field, while the
    # detail page (RiskRegisterSection.tsx) correctly reads the true
    # four-value band straight off the risk API. One click apart, the same
    # assessment could read "High" on the card and "Critical" on the page it
    # opens. risk_band is the fix: the TRUE band (risk/banding.py's band(),
    # via overall_band's own "highest risk, never an average" rule),
    # computed fresh from privacycare_dpia_risk by assessments.py's
    # _risk_bands_by_assessment/_risk_band_for — never stored, never
    # confused with risk_level. Both the card list route and the
    # single-assessment PUT echo populate it; it stays Optional only
    # because PrivacyAssessmentDetailResponse also inherits this field and
    # its own route (_assessment_detail) does not bother computing it — the
    # detail page never reads assessment.risk_band, it reads the risk API
    # directly (RiskRegisterSection.tsx), so there is nothing to compute
    # there.
    risk_band: Optional[str] = None
    system_fides_key: Optional[str] = None
    system_name: Optional[str] = None
    declaration_id: Optional[str] = None
    declaration_name: Optional[str] = None
    data_use: Optional[str] = None
    data_use_name: Optional[str] = None
    data_categories: Optional[List[str]] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TemplateResponse(BaseModel):
    # `key` has NO column in assessment_template (verified against the live
    # schema). The commercial API must derive it. We derive a stable slug from
    # the template name; see template_key() below.
    id: str
    key: str
    version: str
    name: str
    assessment_type: Optional[str] = None
    region: Optional[str] = None
    authority: Optional[str] = None
    legal_reference: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = True


class AssessmentSummaryBlockedGroup(BaseModel):
    # Mirrors AssessmentSummaryBlockedGroup in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. This type is
    # hand-authored in the admin-UI feature module, NOT generated into
    # clients/admin-ui/src/types/api/models/ — do not look for it there.
    name: str
    outdated_count: int
    high_risk_count: int
    total_count: int


class AssessmentSummaryOwner(BaseModel):
    # Mirrors AssessmentSummaryOwner in the same feature types.ts file.
    owner: str
    open_count: int
    outdated_count: int


class AssessmentSummaryResponse(BaseModel):
    # Mirrors AssessmentSummaryResponse in the same feature types.ts file.
    # Fix-round-1 finding: an earlier draft of this endpoint invented a
    # {total, by_status, by_risk_level} shape instead of reading this
    # contract. by_segment buckets into exactly the 4
    # AssessmentSummarySegment values ("completed" | "pending" | "open" |
    # "risk"); see _summary() in api/assessments.py for the derivation,
    # which follows clients/admin-ui/src/mocks/privacy-assessments/
    # compute-summary.ts (the shipped reference implementation).
    total: int
    by_segment: Dict[str, int]
    blocked_groups: List[AssessmentSummaryBlockedGroup]
    owners: List[AssessmentSummaryOwner]


class EvidenceItem(BaseModel):
    # Mirrors EvidenceItem in
    # clients/admin-ui/src/features/privacy-assessments/types.ts.
    #
    # `answer_version.evidence` is JSONB, NOT NULL, server_default '{}' (an
    # empty *object*, not an array) — verified against the live migration
    # (xx_2026_02_05_1500_b2c3d4e5f6g7_add_privacy_assessment_schema.py).
    # No write path in this repo populates it yet (the writer lands in plan
    # 04) and the live `fides` database has zero answer_version rows, so
    # this shape is matched against the UI contract, not against observed
    # data — see task-5-report.md for what was actually checked.
    id: str
    type: str
    value: Optional[str] = None
    created_at: str
    field_name: Optional[str] = None
    source_key: Optional[str] = None
    source_type: Optional[str] = None
    # TS types this `number | null` with no `?` — the key is always present,
    # only its value may be null. No default: an omitted key must fail
    # validation the same way it would fail the UI's required-field parity.
    citation_number: Optional[int]
    data: Optional[Dict] = None


class QuestionEvidence(BaseModel):
    # Mirrors QuestionEvidence in the same feature types.ts file. Exists for
    # AssessmentEvidenceResponse.by_question field-for-field parity; nothing
    # in this plan populates it (see AssessmentEvidenceResponse below).
    question_id: str
    question_text: str
    evidence: List[EvidenceItem]


class AssessmentEvidenceResponse(BaseModel):
    # Mirrors AssessmentEvidenceResponse in the same feature types.ts file.
    # by_question/by_type (driven by GetAssessmentEvidenceParams.group_by)
    # are left unpopulated: useGetAssessmentEvidenceQuery is exported from
    # the RTK slice but no shipped UI component calls it yet (AssessmentDetail
    # derives evidence client-side from question_groups instead), so there is
    # no consumer to validate a grouping implementation against. Only the
    # flat `items` list — the shape every other EvidenceItem consumer
    # (EvidenceCardGroup, EvidenceDrawer) actually reads — is populated here.
    assessment_id: str
    total_count: int
    by_question: Optional[Dict[str, QuestionEvidence]] = None
    by_type: Optional[Dict[str, List[EvidenceItem]]] = None
    items: Optional[List[EvidenceItem]] = None


class AssessmentQuestionResponse(BaseModel):
    # Mirrors AssessmentQuestion in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. Named
    # *Response, not AssessmentQuestion, because AssessmentQuestion is
    # already a SQLAlchemy model in fides.api.models.privacy_assessment.
    #
    # `id` is a DISPLAY LABEL (QuestionCard.tsx:43 renders
    # "{question.id}. {question.question_text}"), sourced from
    # assessment_question.question_key. `question_id` is the real
    # identifier (QuestionCard.tsx:26 passes it as questionId), sourced
    # from assessment_question.id. Do not swap them.
    id: str
    question_id: str
    question_text: str
    # `guidance: string | null;` carries no `?` in the TS contract: the key
    # is always present, only its value may be null. Same precedent as
    # EvidenceItem.citation_number above — no default.
    guidance: Optional[str]
    required: bool
    fides_sources: List[str]
    expected_coverage: str
    answer_text: str
    # `answer_status: AnswerStatus;` / `answer_source: AnswerSource;` are
    # required and non-nullable in the TS contract (no `?`, no `| null`).
    answer_status: str
    answer_source: str
    # `confidence: number | null;` — required, nullable; no default.
    confidence: Optional[float]
    # List[EvidenceItem], not List[dict]: this used to accept the raw JSONB
    # payload unvalidated (fix round 2 finding). EvidenceCardGroup.tsx reads
    # `item.field_name!.replace(...)` off exactly this list — a payload
    # missing a required EvidenceItem field reached the browser as a runtime
    # crash instead of being caught here. Both this field and
    # AssessmentEvidenceResponse.items now go through the same
    # _evidence_items_from_payload() normaliser in api/assessments.py, so a
    # malformed payload is skipped (with the same logged warning) in both
    # places instead of only one.
    evidence: List[EvidenceItem]
    # missing_data IS populated now: generation writes the source keys the
    # record could not supply as a sibling key of the evidence JSONB, and
    # _missing_data_from_payload reads them back (the final-review fix wave
    # — before that it was hardcoded []). sme_prompt still has no source.
    # Both are returned as empty forms rather than omitted when absent: a
    # missing required field breaks the UI's deserialisation exactly as a
    # wrong one would. Both are required, nullable-or-not per the TS
    # contract (no `?` on either).
    missing_data: List[str]
    sme_prompt: Optional[str]


class QuestionGroup(BaseModel):
    # Mirrors QuestionGroup in the same feature types.ts file.
    id: str
    title: str
    requirement_key: str
    questions: List[AssessmentQuestionResponse]
    answered_count: int
    total_count: int
    # No source in the OSS schema — always null for now. Required-but-
    # nullable in the TS contract (no `?`): no default, same precedent as
    # EvidenceItem.citation_number.
    risk_level: Optional[str]
    last_updated_at: Optional[str]
    last_updated_by: Optional[str]


class AssessmentMetadata(BaseModel):
    # Mirrors AssessmentMetadata in the same feature types.ts file. That
    # interface also carries a `[key: string]: unknown` index signature —
    # not a field, and the shared feature-interface parser in
    # test_api_schemas.py does not count it as one (its real field count
    # is 3).
    generation_timestamp: str
    # `model_used: string | null;` — required, nullable; no default.
    model_used: Optional[str]
    use_llm: bool


class PrivacyAssessmentDetailResponse(AssessmentResponse):
    # Mirrors PrivacyAssessmentDetailResponse in the same feature
    # types.ts file, which declares it as
    # `extends PrivacyAssessmentResponse` — its own interface block lists
    # only these four fields, and PrivacyAssessmentResponse's own fields
    # are (for our OSS purposes) AssessmentResponse's fields. This model
    # must carry those four PLUS every AssessmentResponse field via
    # subclassing.
    #
    # Fix round 1: PrivacyAssessmentDetailResponse extends
    # PrivacyAssessmentResponse in TS, NOT the generated AssessmentResponse
    # directly. PrivacyAssessmentResponse narrows two fields off the
    # generated type (`extends Omit<GeneratedAssessmentResponse, "status"
    # | "risk_level">`) and redeclares both as required:
    #   status: AssessmentStatus;              (already str, required,
    #                                            non-nullable here — no
    #                                            change needed)
    #   risk_level: RiskLevel | null;           (required, nullable)
    # The generated AssessmentResponse types risk_level as
    # `risk_level?: string | null` — genuinely optional. Subclassing
    # AssessmentResponse silently inherited THAT optional version instead
    # of the feature-narrowed required one. Override both narrowed fields
    # here so the inheritance matches what this class actually extends,
    # not what it happens to be implemented in terms of.
    status: str
    risk_level: Optional[str]
    assessment_type: str
    question_groups: List[QuestionGroup]
    # The questionnaire is a commercial chat feature with no OSS table.
    # Required, nullable in the TS contract (no `?`) — no default.
    questionnaire: Optional[dict]
    metadata: Optional[AssessmentMetadata]


class AssessmentGroupResponse(BaseModel):
    # Mirrors AssessmentGroupResponse in the same feature types.ts file.
    # `data_use`/`data_use_name` are required-but-nullable (no `?`): no
    # default. `assessments` genuinely carries a `?` in the TS contract,
    # so it alone gets a default.
    data_use: Optional[str]
    data_use_name: Optional[str]
    system_count: int
    assessments: Optional[List[AssessmentResponse]] = None


class UpdateAnswerRequest(BaseModel):
    # Mirrors UpdateAnswerRequest in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. Carries
    # ONLY answer_text — `created_by` deliberately has no field here. It
    # comes from the authenticated principal in api/assessments.py's
    # update_answer route, never from this body, so a client cannot forge
    # authorship in the answer_version audit trail.
    answer_text: str


class UpdateAnswerResponse(BaseModel):
    # Mirrors UpdateAnswerResponse in the same feature types.ts file. All
    # three fields are required and non-nullable in that contract (no `?`,
    # no `| null`) — no defaults here.
    #
    # `question` reuses AssessmentQuestionResponse (the 14-field shape
    # already implemented above for the read surface) rather than defining
    # a second question schema — see that class's own docstring for the
    # id/question_id naming trap.
    #
    # `status` is the ASSESSMENT's status (AssessmentStatus: in_progress |
    # completed | outdated | generating), NOT the answer's. Easy to
    # conflate with AssessmentQuestionResponse.answer_status (AnswerStatus:
    # complete | partial | needs_input) since both fields are named
    # "status"-ish and both ride along in this same response — they are
    # different enums entirely, sourced from different tables
    # (privacy_assessment.status vs answer_version.answer_status).
    question: AssessmentQuestionResponse
    completeness: float
    status: str


class UpdatePrivacyAssessmentRequest(BaseModel):
    # Mirrors UpdatePrivacyAssessmentRequest in
    # clients/admin-ui/src/features/privacy-assessments/types.ts:
    #   { name?: string; status?: AssessmentStatus; risk_level?: RiskLevel; }
    # All three fields carry `?` — genuinely OPTIONAL, not required-but-
    # nullable. `Optional[str] = None` is the right shape for exactly that
    # reason: this is a REQUEST schema (contrast UpdateAnswerResponse.status,
    # a RESPONSE field, which is required-non-nullable with no default) —
    # task 4's brief calls this out by name as the one place the usual
    # "TS `?` -> Pydantic default" / "TS `| null` -> Pydantic Optional, no
    # default" rule doesn't invert.
    #
    # The partial-update mechanism this whole model exists to support:
    # update_assessment (api/assessments.py) calls
    # `request.model_dump(exclude_unset=True)`, so "the client never sent
    # this key" (leave the column untouched) is distinguishable from "the
    # client sent this key with value null" (see the validator below for
    # what each of the three fields does with an explicit null).
    #
    # Fix round 3: `status`/`risk_level` are typed against the AssessmentStatus
    # / RiskLevel enums above rather than bare `str` — see those enums' own
    # comment for the 500-vs-422 reasoning. Pydantic now rejects an
    # out-of-enum label at the schema boundary, before a single statement
    # reaches Postgres, which is what FastAPI turns into the 422 every other
    # malformed body on this surface already gets.
    #
    # `use_enum_values=True` keeps the validated values plain strings, so
    # `request.model_dump(exclude_unset=True)` still hands _update_assessment
    # exactly what it handed it before (bound string parameters for a raw
    # text() UPDATE) — this fix tightens what gets IN, it does not change
    # what goes to the database.
    model_config = ConfigDict(use_enum_values=True)

    name: Optional[str] = None
    status: Optional[AssessmentStatus] = None
    risk_level: Optional[RiskLevel] = None

    @model_validator(mode="after")
    def _reject_explicit_null_for_not_null_columns(
        self,
    ) -> "UpdatePrivacyAssessmentRequest":
        # THE DECISION task 4's brief asks for, stated plainly: privacy_
        # assessment.name and .status are NOT NULL at the DB level (verified
        # against the live schema, and against
        # fides.api.models.privacy_assessment.PrivacyAssessment: both columns
        # declare `nullable=False`) — .risk_level is the only DB-nullable
        # field of the three (`nullable=True`). An explicit `name: null` or
        # `status: null` in the request body can therefore never be applied
        # without violating that constraint. Rather than let that surface as
        # a raw Postgres IntegrityError bubbling out of the route as a 500,
        # it is rejected HERE, at validation time, as a 422 — the same class
        # of client error FastAPI already returns for any other malformed
        # request body.
        #
        # `risk_level: null` is the opposite case and is accepted, applied
        # literally: it clears a previously-set risk level, a legitimate
        # state a DPO can reach (an assessment can go from "high risk" back
        # to "not yet assessed"). See
        # test_update_assessment_explicit_null_risk_level_clears_it.
        #
        # `self.model_fields_set` (not `getattr(self, field) is None` alone)
        # is what makes "sent as null" distinguishable from "never sent" at
        # THIS layer too — a field absent from the body is absent from
        # model_fields_set even though its resolved value is also None (the
        # field's own default).
        for field in ("name", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} may be omitted, but must not be null")
        return self


class AnswerUpdate(BaseModel):
    # Mirrors AnswerUpdate in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. Both
    # fields are required, non-nullable (no `?`) in that contract.
    question_id: str
    answer_text: str


class BulkUpdateAnswersRequest(BaseModel):
    # Mirrors BulkUpdateAnswersRequest in the same feature types.ts file.
    # `created_by` is deliberately absent here too, same reasoning as
    # UpdateAnswerRequest above: authorship comes from the authenticated
    # principal in api/assessments.py's bulk_update_answers route, never
    # from this body.
    answers: List[AnswerUpdate]


class BulkUpdateAnswersResponse(BaseModel):
    # Mirrors BulkUpdateAnswersResponse in the same feature types.ts file.
    # All four fields are required and non-nullable (no `?`, no `| null`)
    # — no defaults here, same precedent as UpdateAnswerResponse.
    #
    # `questions` reuses AssessmentQuestionResponse (not a second question
    # schema) and carries the assessment's FULL question set, not just the
    # entries this batch touched — see _all_questions_response's docstring
    # in api/assessments.py for the evidence (privacy-assessments.slice.ts's
    # bulkUpdateAssessmentAnswers has no onQueryStarted/updateQueryData of
    # its own; it invalidates and lets getAssessment's refetch replace
    # question_groups wholesale, so a subset here would blank out every
    # question the user did not touch in this batch until that refetch
    # lands).
    #
    # `status` is the ASSESSMENT's status, not any answer's — same
    # distinction as UpdateAnswerResponse.status above.
    updated_count: int
    completeness: float
    status: str
    questions: List[AssessmentQuestionResponse]


class DeletePrivacyAssessmentResponse(BaseModel):
    """Task 4's DELETE response envelope.

    NO TypeScript counterpart — deletePrivacyAssessment types its RTK Query
    mutation as `build.mutation<void, string>`
    (privacy-assessments.slice.ts): the admin-UI reads nothing from the
    response body, only `invalidatesTags`. This model exists purely because
    test_every_route_has_a_response_model_declared_or_inferred
    (test_api_registration.py) requires every plus/privacy-assessments
    route, across every HTTP method, to carry a non-None response_model —
    see test_response_model_ts_parity.py's ALLOWLIST entry (ts_name=None)
    for the matching, explicitly-reasoned decision on that side.
    """

    id: str
    deleted: bool = True


class CreateAssessmentTaskRequest(BaseModel):
    # Mirrors clients/admin-ui/src/types/api/models/CreateAssessmentTaskRequest.ts.
    #
    # assessment_types is REQUIRED and must be non-empty: a generation
    # request naming no type has nothing to generate, and accepting it would
    # queue a task that completes instantly having done nothing.
    #
    # The TS union enumerates eight assessment_type values, but the shipped
    # database holds thirteen active templates (four are ROPA types absent
    # from the union) and W2 will add a Kenyan one. Pinning the union here
    # would reject the Kenyan template the moment Carol authors it, so the
    # type stays List[str] and the real check is "an active template
    # exists", made in _resolve_templates where the answer actually lives.
    assessment_types: List[str] = Field(min_length=1)
    system_fides_keys: Optional[List[str]] = None
    use_llm: bool = False
    model: Optional[str] = None
    high_risk_only: bool = False


class CreateAssessmentTaskResponse(BaseModel):
    # The generated TS marks status/message optional; the hand-written
    # override in features/privacy-assessments/types.ts marks both required.
    # Always populating all three satisfies both.
    task_id: str
    status: str
    message: str


class AssessmentTaskSystemInfo(BaseModel):
    # Mirrors clients/admin-ui/src/types/api/models/AssessmentTaskSystemInfo.ts.
    # The feature-folder override calls this TaskSystem and types name as
    # `string | null`; serialising Optional[str] satisfies both readers.
    fides_key: str
    name: Optional[str]


class AssessmentTaskResponse(BaseModel):
    # Mirrors clients/admin-ui/src/types/api/models/AssessmentTaskResponse.ts.
    #
    # Every field is populated on every response even though the generated
    # contract marks most of them optional, because the hand-written
    # override the UI components actually compile against marks them
    # required. The union of the two contracts is "always send everything".
    id: str
    action_type: str
    status: str
    total_count: int
    completed_count: int
    skipped_count: int
    progress: float
    message: Optional[str]
    assessment_types: List[str]
    system_fides_keys: Optional[List[str]]
    systems: Optional[List[AssessmentTaskSystemInfo]]
    created_by: Optional[str]
    use_llm: bool
    llm_model: Optional[str]
    high_risk_only: bool
    assessment_ids: List[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class QuestionnaireSessionStatus(str, Enum):
    # Mirrors the `export enum QuestionnaireSessionStatus` in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. Same three
    # values as chat.py's QUESTIONNAIRE_STATUSES (the questionnaire.status
    # pg_enum labels) — that module's own docstring already pins this set
    # against the database and Ethyca's Python; this pins the same set
    # against the UI contract, and test_the_question_status_enum_matches_
    # the_shipped_contract (test_api_chat.py) asserts the two never drift
    # apart from each other.
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    STOPPED = "stopped"


class QuestionnaireChatMessage(BaseModel):
    # Mirrors QuestionnaireChatMessage in
    # clients/admin-ui/src/features/privacy-assessments/types.ts. Every
    # field but `text`/`is_bot_message` is required-but-nullable in that
    # contract (no `?`, `| null` instead) — no defaults, same precedent as
    # EvidenceItem.citation_number above.
    text: str
    is_bot_message: bool
    sender_email: Optional[str]
    sender_display_name: Optional[str]
    timestamp: Optional[str]
    question_index: Optional[int]


class StartChatRequest(BaseModel):
    # Mirrors StartChatRequest in the same feature types.ts file.
    # `include_question_ids` carries a genuine `?` there (an optional
    # narrowing of which questions this session covers, per chat.py's
    # open_or_resume) — Optional[...] = None is the right shape, same
    # precedent as UpdatePrivacyAssessmentRequest's fields.
    assessment_id: str
    include_question_ids: Optional[List[str]] = None


class StartChatResponse(BaseModel):
    # Mirrors StartChatResponse in the same feature types.ts file. All four
    # fields are required, non-nullable there (no `?`, no `| null`) — no
    # defaults.
    questionnaire_id: str
    assessment_id: str
    messages: List[QuestionnaireChatMessage]
    total_questions: int


class ChatReplyRequest(BaseModel):
    # Mirrors ChatReplyRequest in the same feature types.ts file, WITH ONE
    # DELIBERATE DEVIATION from that contract: the TS interface types
    # `assessment_id` as required (`string`, no `?`), but
    # privacy-assessments.slice.ts's chat mutation strips it before the
    # request ever reaches the wire (`query: ({ assessment_id: _, ...body })
    # => body`) — the officer's assessment_id is not sent on a reply, only
    # on start. Typing this field as Optional[str] = None, against the
    # letter of the TS interface, is therefore the correct read of the
    # ACTUAL contract: "required-but-nullable" is for a key that is always
    # present with a possibly-null value, and this key is not present at
    # all. The questionnaire is looked up by questionnaire_id alone; nothing
    # in api/chat.py reads this field.
    assessment_id: Optional[str] = None
    questionnaire_id: str
    message_text: str


class ChatReplyResponse(BaseModel):
    # Mirrors ChatReplyResponse in the same feature types.ts file. All four
    # fields are required there (no `?`) and `status` is typed against the
    # QuestionnaireSessionStatus enum, not a bare string.
    bot_messages: List[QuestionnaireChatMessage]
    status: QuestionnaireSessionStatus
    answered_questions: int
    total_questions: int


# GroupedAssessmentsResponse is fastapi_pagination.Page[AssessmentGroupResponse].
# Its field set (items/total/page/size/pages) already matches the
# GroupedAssessmentsResponse TS contract field-for-field — see
# test_grouped_assessments_response_matches_the_shipped_contract, which
# asserts that equivalence directly rather than defining a second model.


# Task 1 (config-and-pdf plan): the assessment configuration singleton.
#
# All three models below mirror the same-named interfaces in
# clients/admin-ui/src/features/privacy-assessments/types.ts — the
# HAND-AUTHORED feature-folder file, not the generated
# clients/admin-ui/src/types/api/models/PrivacyAssessmentConfig*.ts trio.
# privacy-assessments.slice.ts imports all three names `from "./types"`
# (the feature folder), so that file — not the generated one, which marks
# several of the same fields differently (e.g. reassessment_enabled/
# reassessment_cron as nullable-and-optional there, non-nullable here) —
# is what the shipped UI actually compiles against.
class PrivacyAssessmentConfigResponse(BaseModel):
    # Every field is required, non-optional in the feature contract (no
    # `?` anywhere) — the four override/Slack fields are required-but-
    # NULLABLE (`string | null`, no default, same precedent as
    # AssessmentMetadata.model_used above); everything else is required
    # and non-nullable.
    id: str
    assessment_model_override: Optional[str]
    chat_model_override: Optional[str]
    # Computed, never stored: override or the platform default. Imported
    # from fides.api.privacycare.llm.DEFAULT_MODEL — the exact constant
    # generation and questionnaire chat both call — never retyped here, so
    # this screen cannot disagree with what the running code actually
    # uses (D-CFG-2).
    effective_assessment_model: str
    effective_chat_model: str
    reassessment_enabled: bool
    reassessment_cron: str
    slack_channel_id: Optional[str]
    slack_channel_name: Optional[str]
    created_at: str
    updated_at: str


class PrivacyAssessmentConfigUpdate(BaseModel):
    # Every field carries `?` in the feature contract — genuinely optional
    # REQUEST fields, same "TS `?` -> Pydantic default" precedent as
    # UpdatePrivacyAssessmentRequest below. The PUT route reads this via
    # `model_dump(exclude_unset=True)`: a field ABSENT from the request
    # body is left alone; a field sent as explicit `null` clears it back to
    # the platform default (D-CFG-3). That distinction is the entire
    # reason this is exclude_unset rather than a plain `.dict()` — Pydantic
    # can't tell "never sent" from "sent as null" any other way.
    #
    # No `questionnaire_tone_prompt` field, deliberately: that column has no
    # TypeScript counterpart (see PrivacyAssessmentConfig, the ORM model,
    # for the same note on the other side) and is never touched by this
    # request or exposed by PrivacyAssessmentConfigResponse above — adding
    # a field for it here would be "helpfully" wiring up a column that was
    # never meant to reach this surface.
    assessment_model_override: Optional[str] = None
    chat_model_override: Optional[str] = None
    reassessment_enabled: Optional[bool] = None
    reassessment_cron: Optional[str] = None
    slack_channel_id: Optional[str] = None
    slack_channel_name: Optional[str] = None

    @model_validator(mode="after")
    def _reject_explicit_null_for_not_null_columns(
        self,
    ) -> "PrivacyAssessmentConfigUpdate":
        # Same reasoning, same shape, as UpdatePrivacyAssessmentRequest's
        # validator of the same name below: reassessment_enabled and
        # reassessment_cron are NOT NULL at the DB level (verified against
        # the live schema and against PrivacyAssessmentConfig, the ORM
        # model — both declare `nullable=False`), so an explicit
        # `reassessment_cron: null` can never be applied without violating
        # that constraint. Rejected here as a 422, before a single
        # statement reaches Postgres, rather than surfacing as a raw
        # IntegrityError / 500.
        #
        # The four override/Slack fields are the opposite case and are NOT
        # covered by this guard: their columns are genuinely nullable, and
        # an explicit null for one of them is the deliberate "clear the
        # override" action D-CFG-3 exists to support.
        for field in ("reassessment_enabled", "reassessment_cron"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} may be omitted, but must not be null")
        return self


class PrivacyAssessmentConfigDefaults(BaseModel):
    # Mirrors the same-named feature-folder interface. All three fields are
    # required there — this is a read-only report of "what the platform
    # would use", always fully populated, never partial.
    default_assessment_model: str
    default_chat_model: str
    default_reassessment_cron: str


def template_key(name: str, id: Optional[str] = None) -> str:
    # Derive the `key` the UI contract requires but the table does not store.
    # Lowercase, non-alphanumerics collapsed to underscores, trimmed.
    #
    # A name that is empty or made entirely of punctuation (`""`, `"!!!"`,
    # `"---"`) collapses to "" here. An empty string satisfies Pydantic's bare
    # `str`, so it would pass silently and every such template would collide
    # on the same key. Guarantee a non-empty, stable result instead: fall
    # back to a slug of `id` (always present, always unique), and only fall
    # back further to a fixed placeholder if even that yields nothing.
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if slug:
        return slug
    if id:
        id_slug = re.sub(r"[^a-z0-9]+", "_", str(id).lower()).strip("_")
        if id_slug:
            return id_slug
    return "template"
