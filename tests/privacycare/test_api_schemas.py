# Our response schemas must match the generated TypeScript types the shipped
# admin-UI is compiled against. These tests parse those .ts files directly, so
# a Fides upgrade that changes the contract fails here rather than in a browser.
import pathlib
import re

from fides.api.privacycare.api.schemas import (
    AssessmentResponse,
    TemplateResponse,
    template_key,
)

TS_DIR = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/types/api/models"
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
