# Our response schemas must match the generated TypeScript types the shipped
# admin-UI is compiled against. These tests parse those .ts files directly, so
# a Fides upgrade that changes the contract fails here rather than in a browser.
import pathlib
import re

from fides.api.privacycare.api.schemas import (
    AssessmentEvidenceResponse,
    AssessmentResponse,
    AssessmentSummaryBlockedGroup,
    AssessmentSummaryOwner,
    AssessmentSummaryResponse,
    EvidenceItem,
    TemplateResponse,
    template_key,
)

TS_DIR = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/types/api/models"
)

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
        for field, optional_marker in re.findall(
            r"^\s*([a-z_]+)(\??):", body, re.M
        )
    }


def _ts_fields(name: str) -> set[str]:
    return set(_ts_field_specs(name).keys())


def _feature_interface_field_specs(name: str) -> dict[str, bool]:
    # Same idea as _ts_field_specs, but for an `export interface Name {...}`
    # block in the hand-authored feature types.ts rather than a generated
    # `export type Name = {...};` file. There is no nested `{` inside these
    # particular interface bodies, so a non-greedy match to the next `}` on
    # its own line is unambiguous.
    text = FEATURE_TS_PATH.read_text()
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", text, re.S)
    assert match, f"{name} not found in {FEATURE_TS_PATH}"
    body = match.group(1)
    return {
        field: bool(optional_marker)
        for field, optional_marker in re.findall(
            r"^\s*([a-z_]+)(\??):", body, re.M
        )
    }


def _feature_interface_fields(name: str) -> set[str]:
    return set(_feature_interface_field_specs(name).keys())


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
    assert set(
        AssessmentSummaryBlockedGroup.model_fields
    ) == _feature_interface_fields("AssessmentSummaryBlockedGroup")


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
        pydantic_required = AssessmentSummaryResponse.model_fields[
            field
        ].is_required()
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
    assert set(EvidenceItem.model_fields) == _feature_interface_fields(
        "EvidenceItem"
    )


def test_evidence_item_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "EvidenceItem"
    ).items():
        pydantic_required = EvidenceItem.model_fields[field].is_required()
        assert pydantic_required == (not is_optional), field


def test_assessment_evidence_response_matches_the_shipped_contract():
    assert set(
        AssessmentEvidenceResponse.model_fields
    ) == _feature_interface_fields("AssessmentEvidenceResponse")


def test_assessment_evidence_response_optionality_matches_the_shipped_contract():
    for field, is_optional in _feature_interface_field_specs(
        "AssessmentEvidenceResponse"
    ).items():
        pydantic_required = AssessmentEvidenceResponse.model_fields[
            field
        ].is_required()
        assert pydantic_required == (not is_optional), field


def test_the_evidence_feature_types_were_actually_read():
    # Same guard as test_the_feature_types_file_was_actually_read: a bad
    # path or regex would make the parity tests above compare empty sets
    # and pass vacuously.
    assert len(_feature_interface_fields("EvidenceItem")) == 9
    assert len(_feature_interface_fields("AssessmentEvidenceResponse")) == 5


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
