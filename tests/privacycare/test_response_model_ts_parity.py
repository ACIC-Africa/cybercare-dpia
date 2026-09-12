# Fix round 2, item 3. The plan's own claim is a three-part chain: every
# route declares a response_model, every response_model has a TypeScript
# counterpart, every counterpart has a parity test. Every other test file in
# this package tests the FIRST link (test_api_registration.py) and, for a
# hand-picked set of models, the THIRD (test_api_schemas.py's
# `..._matches_the_shipped_contract` tests). Nothing ever walked the actual
# response_model graph to check the middle link, or that the third link's
# coverage is complete rather than hand-picked. Demonstrated gap this closes:
# a brand-new route with a brand-new Pydantic model that has no TS
# counterpart at all currently passes the whole suite -- nothing here
# recurses into what a response_model actually contains.
#
# This file recurses into every plus/privacy-assessments route's
# response_model (through Optional/List/Dict/Page wrapping) and asserts,
# for every Pydantic BaseModel reached:
#   1. it has a same-named TS type (generated types/api/models/<Name>.ts)
#      or hand-authored interface (features/privacy-assessments/types.ts),
#      UNLESS it is in ALLOWLIST below with a stated reason; and
#   2. some test in this package's source actually references that TS name
#      (a parity test exists for it), with the same allowlist exception.
#
# Where a model legitimately has no TS counterpart (or no per-instance
# parity test makes sense -- e.g. a generic pagination wrapper), it is
# listed in ALLOWLIST with a reason. That list is the explicit decision this
# item asked for: someone has to edit it for a new gap to pass, so a new gap
# cannot slip through silently the way GET /{assessment_id}/questions did.
import pathlib
import re
import typing

from pydantic import BaseModel

from fides.api.privacycare.asgi import app

TS_DIR = (
    pathlib.Path(__file__).parents[2] / "clients/admin-ui/src/types/api/models"
)
FEATURE_TS_PATH = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/features/privacy-assessments/types.ts"
)

PRIVACYCARE_PATH_PREFIX = "/plus/privacy-assessments"

# Every test module in this package, concatenated, so "a parity test exists
# referencing it" means something a name could plausibly show up in a real
# assertion, not just "the string appears in a comment somewhere in one file
# we picked". This file's own module is excluded — a name appearing only in
# ALLOWLIST/comments here must not count as its own parity test.
_TEST_SOURCE = "\n".join(
    p.read_text()
    for p in sorted(pathlib.Path(__file__).parent.glob("test_*.py"))
    if p != pathlib.Path(__file__)
)

# key: the discovered Pydantic model's own class __name__ (for a
# fastapi_pagination Page[...] instantiation, its generated name, e.g.
# "Page[TemplateResponse]"). value: {"ts_name": <expected TS name, or None
# if there genuinely is no TS counterpart>, "reason": <why>}.
ALLOWLIST: dict[str, dict] = {
    "AssessmentQuestionResponse": {
        "ts_name": "AssessmentQuestion",
        "reason": (
            "AssessmentQuestion is already a SQLAlchemy model name "
            "(fides.api.models.privacy_assessment); this Pydantic response "
            "schema is suffixed *Response to avoid that collision, but the "
            "TS interface it mirrors is the unsuffixed AssessmentQuestion "
            "(see AssessmentQuestionResponse's own docstring in schemas.py)."
        ),
    },
    "Page[TemplateResponse]": {
        "ts_name": None,
        "reason": (
            "fastapi_pagination.Page[T]'s own shape (items/total/page/size/"
            "pages) is generic over T and already proven, once, equal to "
            "the TS pagination contract by "
            "test_grouped_assessments_response_matches_the_shipped_contract "
            "(checked against Page[AssessmentGroupResponse]) — that generic "
            "shape is not re-asserted per T. TemplateResponse (T itself) is "
            "still recursed into and checked on its own below."
        ),
    },
    "Page[AssessmentGroupResponse]": {
        "ts_name": "GroupedAssessmentsResponse",
        "reason": (
            "list_assessments' route paginates as "
            "Page[AssessmentGroupResponse], but the UI contract for it is "
            "the hand-authored GroupedAssessmentsResponse in the feature "
            "types.ts, not a generated Page_AssessmentGroupResponse_ — see "
            "schemas.py's comment on AssessmentGroupResponse and "
            "test_grouped_assessments_response_matches_the_shipped_contract."
        ),
    },
}


def _privacycare_response_models():
    return [
        r.response_model
        for r in app.routes
        if getattr(r, "path", "").startswith(PRIVACYCARE_PATH_PREFIX)
        and getattr(r, "response_model", None) is not None
    ]


def _unwrap_leaf_types(annotation):
    # Descend through Optional[X] (Union[X, None]), List[X], Dict[str, X],
    # and any other generic wrapping to the leaf types underneath. A bare,
    # non-generic annotation (str, int, a BaseModel subclass, ...) yields
    # itself.
    origin = typing.get_origin(annotation)
    if origin is None:
        yield annotation
        return
    for arg in typing.get_args(annotation):
        yield from _unwrap_leaf_types(arg)


def _discover_models(root, seen: set):
    if not (isinstance(root, type) and issubclass(root, BaseModel)):
        return
    if root in seen:
        return
    seen.add(root)
    for field in root.model_fields.values():
        for leaf in _unwrap_leaf_types(field.annotation):
            _discover_models(leaf, seen)


def _all_discovered_models() -> set:
    seen: set = set()
    for model in _privacycare_response_models():
        _discover_models(model, seen)
    return seen


def _ts_generated_file_exists(name: str) -> bool:
    return (TS_DIR / f"{name}.ts").exists()


def _feature_interface_exists(name: str) -> bool:
    text = FEATURE_TS_PATH.read_text()
    return re.search(rf"export interface {re.escape(name)}\b", text) is not None


def _expected_ts_name(model: type) -> "str | None":
    entry = ALLOWLIST.get(model.__name__)
    if entry is not None:
        return entry["ts_name"]
    return model.__name__


def _parity_test_references(ts_name: str) -> bool:
    return re.search(rf'["\']{re.escape(ts_name)}["\']', _TEST_SOURCE) is not None


def test_the_route_walk_finds_a_realistic_number_of_models():
    # Guard: if response_model were never populated, or the recursive walk
    # were broken, every assertion below would pass vacuously over an empty
    # set. Fix round 2's own count is 12 (AssessmentResponse,
    # AssessmentGroupResponse, TemplateResponse, AssessmentSummaryResponse +
    # its 2 nested types, PrivacyAssessmentDetailResponse, QuestionGroup,
    # AssessmentQuestionResponse, EvidenceItem, AssessmentMetadata,
    # AssessmentEvidenceResponse, QuestionEvidence) plus the 2 Page[...]
    # wrapper instantiations themselves.
    models = _all_discovered_models()
    assert len(models) >= 12, sorted(m.__name__ for m in models)


def test_response_model_walk_reaches_the_known_privacycare_models():
    # Guard against the walk quietly degenerating (e.g. only finding Page's
    # own internals) if fastapi_pagination's shape changes upstream.
    names = {m.__name__ for m in _all_discovered_models()}
    expected = {
        "AssessmentResponse",
        "AssessmentGroupResponse",
        "AssessmentSummaryResponse",
        "AssessmentSummaryBlockedGroup",
        "AssessmentSummaryOwner",
        "TemplateResponse",
        "PrivacyAssessmentDetailResponse",
        "QuestionGroup",
        "AssessmentQuestionResponse",
        "EvidenceItem",
        "AssessmentMetadata",
        "AssessmentEvidenceResponse",
        "QuestionEvidence",
    }
    missing = expected - names
    assert not missing, f"models missing from the response_model walk: {missing}"


def test_every_discovered_model_has_a_ts_counterpart_or_an_allowlist_reason():
    failures = []
    for model in _all_discovered_models():
        ts_name = _expected_ts_name(model)
        if ts_name is None:
            continue  # explicitly, with a reason in ALLOWLIST, has none
        if not (_ts_generated_file_exists(ts_name) or _feature_interface_exists(ts_name)):
            failures.append(
                f"{model.__name__}: no generated {ts_name}.ts and no "
                f"`export interface {ts_name}` in {FEATURE_TS_PATH.name} — "
                "add the TS counterpart or an ALLOWLIST entry with a reason"
            )
    assert not failures, "\n".join(failures)


def test_every_ts_counterpart_has_a_referencing_parity_test():
    failures = []
    for model in _all_discovered_models():
        ts_name = _expected_ts_name(model)
        if ts_name is None:
            continue  # ALLOWLISTed as having no counterpart at all
        if not _parity_test_references(ts_name):
            failures.append(
                f"{model.__name__}: no test in tests/privacycare references "
                f"{ts_name!r} — add a parity test asserting field/optionality "
                "equivalence (see test_api_schemas.py for the pattern)"
            )
    assert not failures, "\n".join(failures)


def test_allowlist_entries_all_carry_a_reason():
    # An allowlist someone must edit is a decision; an entry with no reason
    # is silence wearing an allowlist's clothes.
    for name, entry in ALLOWLIST.items():
        assert entry.get("reason"), f"{name}: allowlist entry has no reason"
        assert "ts_name" in entry, f"{name}: allowlist entry has no ts_name key"
