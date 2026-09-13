# Our response schemas must match the generated TypeScript types the shipped
# admin-UI is compiled against. These tests parse those .ts files directly, so
# a Fides upgrade that changes the contract fails here rather than in a browser.
import enum
import pathlib
import re
import typing

from fastapi_pagination import Page

from fides.api.privacycare.api.schemas import (
    AnswerUpdate,
    AssessmentEvidenceResponse,
    AssessmentGroupResponse,
    AssessmentMetadata,
    AssessmentQuestionResponse,
    AssessmentResponse,
    AssessmentStatus,
    AssessmentSummaryBlockedGroup,
    AssessmentSummaryOwner,
    AssessmentSummaryResponse,
    AssessmentTaskResponse,
    AssessmentTaskSystemInfo,
    BulkUpdateAnswersRequest,
    BulkUpdateAnswersResponse,
    ChatReplyResponse,
    CreateAssessmentTaskRequest,
    CreateAssessmentTaskResponse,
    EvidenceItem,
    PrivacyAssessmentDetailResponse,
    QuestionEvidence,
    QuestionGroup,
    QuestionnaireChatMessage,
    RiskLevel,
    StartChatResponse,
    TemplateResponse,
    UpdateAnswerRequest,
    UpdateAnswerResponse,
    UpdatePrivacyAssessmentRequest,
    template_key,
)

TS_DIR = pathlib.Path(__file__).parents[2] / "clients/admin-ui/src/types/api/models"

# AssessmentSummaryResponse (and its two nested types) are NOT
# OpenAPI-generated into TS_DIR above — they are hand-authored directly in
# the admin-UI feature module. Fix round 1 finding: _summary()'s original
# shape was invented rather than read from here, which is exactly the
# failure this file exists to catch. Point at the real source instead.
FEATURE_TS_PATH = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/features/privacy-assessments/types.ts"
)


def _ts_field_specs(name: str) -> dict[str, bool]:
    # Map field name -> True if the TS field is optional (carries a `?`
    # before the colon), False if required.
    text = (TS_DIR / f"{name}.ts").read_text()
    body = re.search(r"=\s*\{(.*?)\};", text, re.S).group(1)
    return {
        field: bool(optional_marker)
        for field, optional_marker in re.findall(r"^\s*([a-z_]+)(\??):", body, re.M)
    }


def _ts_fields(name: str) -> set[str]:
    return set(_ts_field_specs(name).keys())


def _feature_interface_field_specs(name: str) -> dict[str, bool]:
    # Same idea as _ts_field_specs, but for an `export interface Name {...}`
    # block in the hand-authored feature types.ts rather than a generated
    # `export type Name = {...};` file. There is no nested `{` inside these
    # particular interface bodies, so a non-greedy match to the next `}` on
    # its own line is unambiguous.
    #
    # `[^{]*` (rather than a literal " ") between the name and the opening
    # brace so this also matches `export interface Name extends Other {`
    # (PrivacyAssessmentDetailResponse) and not just the plain
    # `export interface Name {` form.
    text = FEATURE_TS_PATH.read_text()
    match = re.search(rf"export interface {name}\b[^{{]*\{{(.*?)\n\}}", text, re.S)
    assert match, f"{name} not found in {FEATURE_TS_PATH}"
    body = match.group(1)
    return {
        field: bool(optional_marker)
        for field, optional_marker in re.findall(r"^\s*([a-z_]+)(\??):", body, re.M)
    }


def _feature_interface_fields(name: str) -> set[str]:
    return set(_feature_interface_field_specs(name).keys())


def _feature_interface_raw_specs(name: str) -> dict[str, str]:
    # Same interface-body match as _feature_interface_field_specs, but
    # returns the raw TS type text per field (e.g. "string | null") rather
    # than just the optional marker. A trailing `?` alone does not tell you
    # whether a REQUIRED field's value may be null — that is a separate
    # axis, and conflating the two is exactly how the risk_level narrowing
    # bug (fix round 1) went undetected: the optionality test only ever
    # asked whether a default existed.
    text = FEATURE_TS_PATH.read_text()
    match = re.search(rf"export interface {name}\b[^{{]*\{{(.*?)\n\}}", text, re.S)
    assert match, f"{name} not found in {FEATURE_TS_PATH}"
    body = match.group(1)
    return {
        field: type_text.strip()
        for field, type_text in re.findall(
            r"^\s*([a-z_]+)\??:\s*(.+?);\s*$", body, re.M
        )
    }


def _ts_type_is_nullable(type_text: str) -> bool:
    return "null" in [part.strip() for part in type_text.split("|")]


def _assert_admits_none_where_ts_nullable(model, raw_specs, fields=None):
    # For every TS field typed `X | null`, assert the Pydantic annotation
    # actually admits None. Optionality (is_required()) alone would let a
    # required-but-nullable field stay wrongly typed as non-nullable and go
    # unnoticed. `fields`, if given, restricts the check to that subset
    # (used when raw_specs comes from a different interface than the one
    # being asserted against, e.g. checking narrowed fields on a subclass).
    for field, type_text in raw_specs.items():
        if fields is not None and field not in fields:
            continue
        if not _ts_type_is_nullable(type_text):
            continue
        annotation = model.model_fields[field].annotation
        assert type(None) in typing.get_args(annotation), (
            f"{field}: TS type {type_text!r} is nullable but Pydantic "
            f"annotation {annotation!r} does not admit None"
        )


def _narrowed_fields(name: str) -> set[str]:
    # PrivacyAssessmentResponse narrows fields off the generated
    # AssessmentResponse via `extends Omit<GeneratedAssessmentResponse,
    # "status" | "risk_level">`. Parse the quoted field names out of that
    # clause directly rather than hardcoding them, so a future narrowing
    # addition is caught automatically instead of drifting silently (fix
    # round 1 finding: risk_level's narrowed, required-nullable contract
    # was missed because nothing traced this extends clause through).
    text = FEATURE_TS_PATH.read_text()
    match = re.search(rf"export interface {name}\b(.*?)\{{", text, re.S)
    assert match, f"{name} extends-clause not found in {FEATURE_TS_PATH}"
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def _ts_enum_values(name: str) -> set[str]:
    # Parse an `export enum Name { KEY = "value", ... }` block out of the
    # hand-authored feature types.ts. Fix round 3 (whole-range review, MAJOR
    # finding): UpdatePrivacyAssessmentRequest.status/risk_level are now
    # typed against Python enums, and the ONLY thing that keeps those
    # members from drifting away from what the UI can actually send is this
    # parse — the same "read the shipped contract, don't restate it"
    # discipline every other test in this file already applies to
    # interfaces.
    text = FEATURE_TS_PATH.read_text()
    match = re.search(rf"export enum {name}\s*\{{(.*?)\n\}}", text, re.S)
    assert match, f"{name} enum not found in {FEATURE_TS_PATH}"
    return set(re.findall(r'=\s*"([^"]+)"', match.group(1)))


def _ts_named_type(type_text: str) -> str | None:
    # The non-null, non-array part of a TS field type, if it is a single
    # named type: "AssessmentStatus" -> "AssessmentStatus",
    # "RiskLevel | null" -> "RiskLevel", "AnswerUpdate[]" -> "AnswerUpdate",
    # "string" -> "string".
    parts = [p.strip() for p in type_text.split("|") if p.strip() != "null"]
    if len(parts) != 1:
        return None
    return parts[0].removesuffix("[]").strip()


def _assert_enum_typed_where_ts_is_an_enum(model, name):
    # THE type-awareness gap finding 6 names: the field-set and optionality
    # tests compare names and `?` markers only, never TYPES — which is
    # exactly how `status: Optional[str]` sat unnoticed against
    # `status?: AssessmentStatus` until an "archived" value 500'd out of
    # Postgres. For every field whose TS type is one of the enums declared
    # in the same types.ts file, assert the Pydantic annotation is a real
    # Python Enum whose values match that TS enum member-for-member.
    text = FEATURE_TS_PATH.read_text()
    ts_enum_names = set(re.findall(r"export enum ([A-Za-z0-9_]+)", text))
    checked = []
    for field, type_text in _feature_interface_raw_specs(name).items():
        named = _ts_named_type(type_text)
        if named not in ts_enum_names:
            continue
        annotation = model.model_fields[field].annotation
        candidates = [annotation, *typing.get_args(annotation)]
        enums = [
            c for c in candidates if isinstance(c, type) and issubclass(c, enum.Enum)
        ]
        assert enums, (
            f"{name}.{field}: TS types this as the enum {named!r}, but the "
            f"Pydantic annotation {annotation!r} is not an Enum — a value "
            f"outside {named} would pass validation and reach the database"
        )
        assert {member.value for member in enums[0]} == _ts_enum_values(named), (
            f"{name}.{field}: {enums[0].__name__} members have drifted from "
            f"the shipped {named} enum"
        )
        checked.append(field)
    return checked


def test_assessment_response_matches_the_shipped_contract():
    assert set(AssessmentResponse.model_fields) == _ts_fields("AssessmentResponse")


def test_template_response_matches_the_shipped_contract():
    assert set(TemplateResponse.model_fields) == _ts_fields("TemplateResponse")


def test_assessment_response_optionality_matches_the_shipped_contract():
    # Field-name parity alone would let a schema where every field became
    # Optional pass while returning nulls the UI cannot handle. Compare
    # required-ness too: a TS field with no `?` must be a required Pydantic
    # field, and one with `?` must not be.
    for field, is_optional in _ts_field_specs("AssessmentResponse").items():
        pydantic_required = AssessmentResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_template_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _ts_field_specs("TemplateResponse").items():
        pydantic_required = TemplateResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_ts_files_were_actually_read():
    # Guard: a bad path would make both tests above compare empty sets. An
    # exact count (not just >= N) also catches a regex that silently trimmed
    # a few fields while both sides shrank together — that must fail loudly
    # if a future Fides upgrade changes the contract.
    assert len(_ts_fields("AssessmentResponse")) == 17
    assert len(_ts_fields("TemplateResponse")) == 10


def test_summary_response_matches_the_shipped_contract():
    # Fix round 1: the original _summary() shape ({total, by_status,
    # by_risk_level}) was never checked against this file. This is the guard
    # that stops that drifting again.
    assert set(AssessmentSummaryResponse.model_fields) == _feature_interface_fields(
        "AssessmentSummaryResponse"
    )


def test_summary_blocked_group_matches_the_shipped_contract():
    assert set(AssessmentSummaryBlockedGroup.model_fields) == _feature_interface_fields(
        "AssessmentSummaryBlockedGroup"
    )


def test_summary_owner_matches_the_shipped_contract():
    assert set(AssessmentSummaryOwner.model_fields) == _feature_interface_fields(
        "AssessmentSummaryOwner"
    )


def test_summary_response_optionality_matches_the_shipped_contract():
    # None of AssessmentSummaryResponse's fields carry a `?` in the shipped
    # contract — every one must be a required Pydantic field, not Optional.
    for field, is_optional in _feature_interface_field_specs(
        "AssessmentSummaryResponse"
    ).items():
        pydantic_required = AssessmentSummaryResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_feature_types_file_was_actually_read():
    # Same guard as test_the_ts_files_were_actually_read, for the
    # hand-authored feature file: a bad path or regex would make the parity
    # tests above compare empty sets and pass vacuously.
    assert len(_feature_interface_fields("AssessmentSummaryResponse")) == 4
    assert len(_feature_interface_fields("AssessmentSummaryBlockedGroup")) == 4
    assert len(_feature_interface_fields("AssessmentSummaryOwner")) == 3


def test_evidence_item_matches_the_shipped_contract():
    # Plan 05's brief told an implementer to return an empty list from the
    # evidence endpoint and "not invent an evidence table" — that guidance
    # was wrong (evidence lives on answer_version.evidence) and inventing a
    # response shape instead of reading EvidenceItem here was caught twice
    # already in this plan. This is the guard against a third invented shape.
    assert set(EvidenceItem.model_fields) == _feature_interface_fields("EvidenceItem")


def test_evidence_item_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs("EvidenceItem").items():
        pydantic_required = EvidenceItem.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_assessment_evidence_response_matches_the_shipped_contract():
    assert set(AssessmentEvidenceResponse.model_fields) == _feature_interface_fields(
        "AssessmentEvidenceResponse"
    )


def test_assessment_evidence_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "AssessmentEvidenceResponse"
    ).items():
        pydantic_required = AssessmentEvidenceResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_question_evidence_matches_the_shipped_contract():
    # Fix round 2 gap, surfaced by walking every response_model's nested
    # types (test_response_model_ts_parity.py): QuestionEvidence is reachable
    # from AssessmentEvidenceResponse.by_question and has a same-named TS
    # counterpart in the feature types file, but nothing asserted the two
    # matched until now — schemas.py's own comment on QuestionEvidence only
    # noted nothing populates `by_question` yet, not that the type itself
    # was untested.
    assert set(QuestionEvidence.model_fields) == _feature_interface_fields(
        "QuestionEvidence"
    )


def test_question_evidence_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "QuestionEvidence"
    ).items():
        pydantic_required = QuestionEvidence.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_evidence_feature_types_were_actually_read():
    # Same guard as test_the_feature_types_file_was_actually_read: a bad
    # path or regex would make the parity tests above compare empty sets
    # and pass vacuously.
    assert len(_feature_interface_fields("EvidenceItem")) == 9
    assert len(_feature_interface_fields("AssessmentEvidenceResponse")) == 5
    assert len(_feature_interface_fields("QuestionEvidence")) == 3


def test_assessment_question_response_matches_the_shipped_contract():
    # The TS interface is named AssessmentQuestion; our Pydantic model is
    # named AssessmentQuestionResponse to avoid colliding with the
    # SQLAlchemy model of the same (unsuffixed) name.
    assert set(AssessmentQuestionResponse.model_fields) == _feature_interface_fields(
        "AssessmentQuestion"
    )


def test_assessment_question_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "AssessmentQuestion"
    ).items():
        pydantic_required = AssessmentQuestionResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        AssessmentQuestionResponse, _feature_interface_raw_specs("AssessmentQuestion")
    )


def test_question_group_matches_the_shipped_contract():
    assert set(QuestionGroup.model_fields) == _feature_interface_fields("QuestionGroup")


def test_question_group_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs("QuestionGroup").items():
        pydantic_required = QuestionGroup.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        QuestionGroup, _feature_interface_raw_specs("QuestionGroup")
    )


def test_assessment_metadata_matches_the_shipped_contract():
    # AssessmentMetadata also carries a `[key: string]: unknown` index
    # signature in TS. It must not be counted as a field: the parser's
    # field regex requires the line to start with `[a-z_]+`, which a `[`
    # never matches, so the index signature is already excluded and the
    # real field count is 3 (see test_the_feature_types_file_was_actually_read
    # -equivalent guard below).
    assert set(AssessmentMetadata.model_fields) == _feature_interface_fields(
        "AssessmentMetadata"
    )


def test_assessment_metadata_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "AssessmentMetadata"
    ).items():
        pydantic_required = AssessmentMetadata.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        AssessmentMetadata, _feature_interface_raw_specs("AssessmentMetadata")
    )


def test_assessment_group_response_matches_the_shipped_contract():
    assert set(AssessmentGroupResponse.model_fields) == _feature_interface_fields(
        "AssessmentGroupResponse"
    )


def test_assessment_group_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "AssessmentGroupResponse"
    ).items():
        pydantic_required = AssessmentGroupResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        AssessmentGroupResponse, _feature_interface_raw_specs("AssessmentGroupResponse")
    )


def test_grouped_assessments_response_matches_the_shipped_contract():
    # GroupedAssessmentsResponse is fastapi_pagination.Page[AssessmentGroupResponse]
    # — assert the field-set equivalence directly rather than defining a
    # second model.
    assert set(Page.model_fields) == _feature_interface_fields(
        "GroupedAssessmentsResponse"
    )


def test_privacy_assessment_detail_response_matches_the_shipped_contract():
    # PrivacyAssessmentDetailResponse `extends PrivacyAssessmentResponse` in
    # TS, so its own interface block lists only the four ADDED fields. Our
    # Pydantic model must carry those four PLUS every AssessmentResponse
    # field (PrivacyAssessmentResponse only narrows two already-typed
    # fields; it adds none). This is the inheritance check the brief calls
    # out as most likely to be got wrong.
    added_fields = _feature_interface_fields("PrivacyAssessmentDetailResponse")
    assert added_fields == {
        "assessment_type",
        "question_groups",
        "questionnaire",
        "metadata",
    }
    assert set(PrivacyAssessmentDetailResponse.model_fields) == (
        added_fields | set(AssessmentResponse.model_fields)
    )


def test_privacy_assessment_detail_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "PrivacyAssessmentDetailResponse"
    ).items():
        pydantic_required = PrivacyAssessmentDetailResponse.model_fields[
            field
        ].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        PrivacyAssessmentDetailResponse,
        _feature_interface_raw_specs("PrivacyAssessmentDetailResponse"),
    )


def test_privacy_assessment_detail_response_narrowed_fields_match_the_feature_contract():
    # Fix round 1: PrivacyAssessmentDetailResponse actually extends
    # PrivacyAssessmentResponse in TS, not the generated AssessmentResponse
    # directly, and PrivacyAssessmentResponse narrows status/risk_level to
    # required, redeclared types via `extends Omit<GeneratedAssessmentResponse,
    # "status" | "risk_level">`. Parse that narrowing clause to find the
    # affected fields rather than hardcoding "status"/"risk_level" here, so
    # a future narrowing addition is caught automatically instead of
    # drifting silently the way risk_level's requiredness did.
    narrowed = _narrowed_fields("PrivacyAssessmentResponse")
    assert narrowed  # guard against a silently-empty parse
    narrowed_specs = _feature_interface_field_specs("PrivacyAssessmentResponse")
    for field in narrowed:
        is_optional = narrowed_specs[field]
        pydantic_required = PrivacyAssessmentDetailResponse.model_fields[
            field
        ].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        PrivacyAssessmentDetailResponse,
        _feature_interface_raw_specs("PrivacyAssessmentResponse"),
        fields=narrowed,
    )


def test_the_dpia_envelope_feature_types_were_actually_read():
    # Same guard as test_the_feature_types_file_was_actually_read: a bad
    # path or regex would make the parity tests above compare empty sets
    # and pass vacuously.
    assert len(_feature_interface_fields("AssessmentQuestion")) == 14
    assert len(_feature_interface_fields("QuestionGroup")) == 9
    assert len(_feature_interface_fields("AssessmentMetadata")) == 3
    assert len(_feature_interface_fields("AssessmentGroupResponse")) == 4
    assert len(_feature_interface_fields("GroupedAssessmentsResponse")) == 5
    assert len(_feature_interface_fields("PrivacyAssessmentDetailResponse")) == 4
    assert len(_feature_interface_fields("UpdateAnswerResponse")) == 3
    assert len(_feature_interface_fields("BulkUpdateAnswersResponse")) == 4


def test_update_answer_response_matches_the_shipped_contract():
    # Task 2: UpdateAnswerResponse mirrors the same-named interface in
    # types.ts — {question, completeness, status}. `question` reuses
    # AssessmentQuestionResponse (asserted above to match AssessmentQuestion
    # field-for-field) rather than a second question schema.
    assert set(UpdateAnswerResponse.model_fields) == _feature_interface_fields(
        "UpdateAnswerResponse"
    )


def test_update_answer_response_optionality_matches_the_shipped_contract():
    # All three fields are required and non-nullable in the shipped
    # contract (no `?`, no `| null`) — none carry a Pydantic default.
    for field, is_optional in _feature_interface_field_specs(
        "UpdateAnswerResponse"
    ).items():
        pydantic_required = UpdateAnswerResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        UpdateAnswerResponse, _feature_interface_raw_specs("UpdateAnswerResponse")
    )


def test_bulk_update_answers_response_matches_the_shipped_contract():
    # Task 3: BulkUpdateAnswersResponse mirrors the same-named interface in
    # types.ts — {updated_count, completeness, status, questions}.
    # `questions` reuses AssessmentQuestionResponse (asserted above to match
    # AssessmentQuestion field-for-field) rather than a second question
    # schema — same precedent as UpdateAnswerResponse.question.
    assert set(BulkUpdateAnswersResponse.model_fields) == _feature_interface_fields(
        "BulkUpdateAnswersResponse"
    )


def test_bulk_update_answers_response_optionality_matches_the_shipped_contract():
    # All four fields are required and non-nullable in the shipped contract
    # (no `?`, no `| null`) — none carry a Pydantic default.
    for field, is_optional in _feature_interface_field_specs(
        "BulkUpdateAnswersResponse"
    ).items():
        pydantic_required = BulkUpdateAnswersResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        BulkUpdateAnswersResponse,
        _feature_interface_raw_specs("BulkUpdateAnswersResponse"),
    )


def test_create_assessment_task_response_matches_the_generated_contract():
    # Task 6: CreateAssessmentTaskResponse has BOTH a generated TS type
    # (CreateAssessmentTaskResponse.ts, task_id/status?/message?) and a
    # hand-written override in this feature file (all three required) — the
    # two contracts disagree only on optionality, never on field names, so
    # one field-set assertion covers both sides.
    assert set(CreateAssessmentTaskResponse.model_fields) == _ts_fields(
        "CreateAssessmentTaskResponse"
    )


def test_create_assessment_task_response_matches_the_feature_contract():
    assert set(CreateAssessmentTaskResponse.model_fields) == _feature_interface_fields(
        "CreateAssessmentTaskResponse"
    )


def test_create_assessment_task_response_optionality_matches_the_feature_contract():
    # The generated type marks status/message optional; the hand-written
    # override the UI components actually compile against marks all three
    # required. Schemas.py's own docstring on this model states the
    # resolution: always populate all three, which satisfies the generated
    # type's weaker (optional) contract while matching the feature file's
    # stricter (required) one exactly — so required-ness is checked against
    # the feature file, not the generated file.
    for field, is_optional in _feature_interface_field_specs(
        "CreateAssessmentTaskResponse"
    ).items():
        pydantic_required = CreateAssessmentTaskResponse.model_fields[
            field
        ].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_create_assessment_task_response_contracts_were_actually_read():
    assert len(_ts_fields("CreateAssessmentTaskResponse")) == 3
    assert len(_feature_interface_fields("CreateAssessmentTaskResponse")) == 3


def test_create_assessment_task_request_matches_the_generated_contract():
    # Task 7: CreateAssessmentTaskRequest has no feature-file override — the
    # feature types.ts file re-exports the generated type directly
    # (`export type { CreateAssessmentTaskRequest, ... }`) rather than
    # redeclaring it, so the generated file is the only contract to pin
    # against.
    assert set(CreateAssessmentTaskRequest.model_fields) == _ts_fields(
        "CreateAssessmentTaskRequest"
    )


def test_the_create_assessment_task_request_contract_was_actually_read():
    assert len(_ts_fields("CreateAssessmentTaskRequest")) == 5


def test_assessment_task_response_matches_the_generated_contract():
    # Parity is pinned against the GENERATED file, not the feature-folder
    # override: the generated file is Ethyca's published OpenAPI contract,
    # so matching it means a future regeneration produces no diff against
    # us. The feature override (`AssessmentTaskResponse` in
    # features/privacy-assessments/types.ts) is missing `high_risk_only`
    # entirely — see
    # test_assessment_task_response_keeps_high_risk_only_the_feature_override_omits
    # below — so asserting equality against it here would fail on a field
    # this schema is required to keep.
    assert set(AssessmentTaskResponse.model_fields) == _ts_fields(
        "AssessmentTaskResponse"
    )


def test_assessment_task_response_keeps_high_risk_only_the_feature_override_omits():
    # Explicit decision from the task brief: AssessmentTaskResponse keeps
    # high_risk_only (present in the generated contract) even though the
    # hand-written feature override omits it. Populate-every-field wins over
    # narrowing to the feature file's subset.
    assert "high_risk_only" in AssessmentTaskResponse.model_fields
    assert "high_risk_only" in _ts_fields("AssessmentTaskResponse")
    assert "high_risk_only" not in _feature_interface_fields("AssessmentTaskResponse")


def test_the_assessment_task_response_contract_was_actually_read():
    assert len(_ts_fields("AssessmentTaskResponse")) == 17


def test_assessment_task_system_info_matches_the_generated_contract():
    assert set(AssessmentTaskSystemInfo.model_fields) == _ts_fields(
        "AssessmentTaskSystemInfo"
    )


def test_the_assessment_task_system_info_contract_was_actually_read():
    assert len(_ts_fields("AssessmentTaskSystemInfo")) == 2


def test_update_privacy_assessment_request_matches_the_shipped_contract():
    # Task 4: UpdatePrivacyAssessmentRequest mirrors the same-named
    # interface in the feature types.ts file — {name?, status?, risk_level?}.
    assert set(
        UpdatePrivacyAssessmentRequest.model_fields
    ) == _feature_interface_fields("UpdatePrivacyAssessmentRequest")


def test_update_privacy_assessment_request_optionality_matches_the_shipped_contract():
    # All three fields carry `?` in the shipped contract — genuinely
    # optional REQUEST fields, not required-but-nullable (see the model's
    # own docstring in schemas.py for why this is the one place the usual
    # TS-`?`/TS-`| null` mapping inverts).
    for field, is_optional in _feature_interface_field_specs(
        "UpdatePrivacyAssessmentRequest"
    ).items():
        pydantic_required = UpdatePrivacyAssessmentRequest.model_fields[
            field
        ].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_update_privacy_assessment_request_feature_type_was_actually_read():
    assert len(_feature_interface_fields("UpdatePrivacyAssessmentRequest")) == 3


def test_assessment_status_enum_matches_the_shipped_contract():
    # Fix round 3: the Python enum UpdatePrivacyAssessmentRequest.status is
    # typed against must carry exactly the values the UI's AssessmentStatus
    # can send — no more (the UI could not produce it) and no fewer (a
    # legitimate value would 422). Both sets also match the live Postgres
    # `assessmentstatus` labels.
    assert {s.value for s in AssessmentStatus} == _ts_enum_values("AssessmentStatus")


def test_risk_level_enum_matches_the_shipped_contract():
    assert {r.value for r in RiskLevel} == _ts_enum_values("RiskLevel")


def test_the_shipped_enums_were_actually_read():
    assert len(_ts_enum_values("AssessmentStatus")) == 4
    assert len(_ts_enum_values("RiskLevel")) == 3


def test_update_privacy_assessment_request_enum_fields_are_enum_typed():
    # The type-aware half of parity, and the specific hole finding 1 of the
    # whole-range review fell through: `status?: AssessmentStatus` used to be
    # satisfied by `Optional[str]`, so an out-of-enum label reached a native
    # Postgres enum column and surfaced as a 500.
    checked = _assert_enum_typed_where_ts_is_an_enum(
        UpdatePrivacyAssessmentRequest, "UpdatePrivacyAssessmentRequest"
    )
    assert set(checked) == {"status", "risk_level"}, (
        "both enum-typed fields of this request must be covered by the "
        f"type-aware check, got {checked}"
    )


def test_update_answer_request_matches_the_shipped_contract():
    # Finding 6 of the whole-range review: of the four request models added
    # by tasks 2-4, only UpdatePrivacyAssessmentRequest had parity tests.
    # UpdateAnswerRequest carries ONLY answer_text — `created_by` must never
    # appear here, since authorship comes from the authenticated principal
    # (see the model's own comment in schemas.py). A field-set equality
    # assertion pins both directions at once.
    assert set(UpdateAnswerRequest.model_fields) == _feature_interface_fields(
        "UpdateAnswerRequest"
    )


def test_update_answer_request_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "UpdateAnswerRequest"
    ).items():
        pydantic_required = UpdateAnswerRequest.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        UpdateAnswerRequest, _feature_interface_raw_specs("UpdateAnswerRequest")
    )


def test_answer_update_matches_the_shipped_contract():
    # The one the review calls out by name: a rename of
    # AnswerUpdate.question_id in types.ts used to pass every test in this
    # suite while breaking every bulk save.
    assert set(AnswerUpdate.model_fields) == _feature_interface_fields("AnswerUpdate")


def test_answer_update_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs("AnswerUpdate").items():
        pydantic_required = AnswerUpdate.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        AnswerUpdate, _feature_interface_raw_specs("AnswerUpdate")
    )


def test_bulk_update_answers_request_matches_the_shipped_contract():
    assert set(BulkUpdateAnswersRequest.model_fields) == _feature_interface_fields(
        "BulkUpdateAnswersRequest"
    )


def test_bulk_update_answers_request_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "BulkUpdateAnswersRequest"
    ).items():
        pydantic_required = BulkUpdateAnswersRequest.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field
    _assert_admits_none_where_ts_nullable(
        BulkUpdateAnswersRequest,
        _feature_interface_raw_specs("BulkUpdateAnswersRequest"),
    )


def test_bulk_update_answers_request_element_type_is_the_shipped_answer_update():
    # `answers: AnswerUpdate[]` in TS. Field-set parity alone would pass with
    # `List[dict]` or a list of the wrong model, so assert the element type
    # itself — the same type-blindness finding 6 names, on the one request
    # field where it carries a nested contract.
    element = typing.get_args(
        BulkUpdateAnswersRequest.model_fields["answers"].annotation
    )
    assert element and element[0] is AnswerUpdate
    assert _feature_interface_raw_specs("BulkUpdateAnswersRequest")["answers"] == (
        "AnswerUpdate[]"
    )


def test_the_request_model_feature_types_were_actually_read():
    assert len(_feature_interface_fields("UpdateAnswerRequest")) == 1
    assert len(_feature_interface_fields("AnswerUpdate")) == 2
    assert len(_feature_interface_fields("BulkUpdateAnswersRequest")) == 1


def test_template_key_derives_a_slug_from_a_normal_name():
    assert template_key("GDPR Assessment") == "gdpr_assessment"


def test_template_key_strips_leading_and_trailing_punctuation():
    assert template_key("  GDPR! Assessment?? ") == "gdpr_assessment"


def test_template_key_falls_back_to_id_when_name_is_entirely_punctuation():
    # "!!!" and "---" collapse to "" after stripping non-alphanumerics; an
    # empty string satisfies Pydantic's bare `str` silently, so every such
    # template would collide on the same key. Must fall back to a non-empty,
    # distinguishing value derived from the id instead.
    assert template_key("!!!", id="tmpl-abc-123") == "tmpl_abc_123"
    assert template_key("---", id="tmpl-xyz-789") == "tmpl_xyz_789"
    result = template_key("!!!")
    assert result  # no id given: still must not be empty


def test_template_key_falls_back_to_id_when_name_is_empty():
    assert template_key("", id="tmpl-42") == "tmpl_42"
    result = template_key("")
    assert result  # no id given: still must not be empty


def test_template_key_is_never_empty():
    for name, id_ in [("", None), ("!!!", None), ("---", None), ("", "")]:
        assert template_key(name, id=id_) != ""


# --- Questionnaire chat contracts (task 4) ---
#
# QuestionnaireChatMessage, StartChatResponse and ChatReplyResponse are all
# hand-authored interfaces in the same FEATURE_TS_PATH file this module
# already reads for AssessmentSummaryResponse etc., so _feature_interface_
# fields/_feature_interface_field_specs (defined above, same helpers
# test_api_chat.py imports from this module) are the right tool — no new
# parsing helper needed. These three models are also exercised end-to-end
# by the questionnaire-chat routes themselves in test_api_chat.py, whose
# own field-set parity tests these mirror; this file remains the canonical
# home for the "does this Pydantic model equal the shipped .ts contract"
# question, the same as every other model above.


def test_start_chat_response_matches_the_shipped_contract():
    assert set(StartChatResponse.model_fields) == _feature_interface_fields(
        "StartChatResponse"
    )


def test_start_chat_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "StartChatResponse"
    ).items():
        pydantic_required = StartChatResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_chat_reply_response_matches_the_shipped_contract():
    assert set(ChatReplyResponse.model_fields) == _feature_interface_fields(
        "ChatReplyResponse"
    )


def test_chat_reply_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "ChatReplyResponse"
    ).items():
        pydantic_required = ChatReplyResponse.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_questionnaire_chat_message_matches_the_shipped_contract():
    assert set(QuestionnaireChatMessage.model_fields) == _feature_interface_fields(
        "QuestionnaireChatMessage"
    )


def test_questionnaire_chat_message_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "QuestionnaireChatMessage"
    ).items():
        pydantic_required = QuestionnaireChatMessage.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_chat_feature_types_were_actually_read():
    assert len(_feature_interface_fields("StartChatResponse")) == 4
    assert len(_feature_interface_fields("ChatReplyResponse")) == 4
    assert len(_feature_interface_fields("QuestionnaireChatMessage")) == 6
