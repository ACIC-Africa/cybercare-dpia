# Our response schemas must match the generated TypeScript types the shipped
# admin-UI is compiled against. These tests parse those .ts files directly, so
# a Fides upgrade that changes the contract fails here rather than in a browser.
import pathlib
import re

from fides.api.privacycare.api.schemas import AssessmentResponse, TemplateResponse

TS_DIR = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/types/api/models"
)


def _ts_fields(name: str) -> set[str]:
    text = (TS_DIR / f"{name}.ts").read_text()
    body = re.search(r"=\s*\{(.*?)\};", text, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+)\??:", body, re.M))


def test_assessment_response_matches_the_shipped_contract():
    assert set(AssessmentResponse.model_fields) == _ts_fields("AssessmentResponse")


def test_template_response_matches_the_shipped_contract():
    assert set(TemplateResponse.model_fields) == _ts_fields("TemplateResponse")


def test_the_ts_files_were_actually_read():
    # Guard: a bad path would make both tests above compare empty sets.
    assert len(_ts_fields("AssessmentResponse")) >= 15
