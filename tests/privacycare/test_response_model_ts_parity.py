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

from fides.api.privacycare.api.router import (
    PRIVACYCARE_CHAT_PREFIX,
    PRIVACYCARE_DSR_PREFIX,
    PRIVACYCARE_GROUNDS_PREFIX,
    PRIVACYCARE_MONITORS_PREFIX,
    PRIVACYCARE_PREFIX,
    PRIVACYCARE_PROCESSES_PREFIX,
)
from fides.api.privacycare.asgi import app

TS_DIR = (
    pathlib.Path(__file__).parents[2] / "clients/admin-ui/src/types/api/models"
)
# Every hand-authored feature file that may hold a counterpart interface, not
# just one. I5: this was a single path (privacy-assessments/types.ts), so a
# response model whose TS interface lived anywhere else — e.g. the Kenyan
# grounds surface, whose types are hand-written in
# features/privacycare/processing-grounds.slice.ts — was invisible to the
# check and could only be got past the gate by allowlisting it. The glob
# keeps a future PrivacyCare slice covered without another edit here.
FEATURE_TS_PATH = (
    pathlib.Path(__file__).parents[2]
    / "clients/admin-ui/src/features/privacy-assessments/types.ts",
    *sorted(
        (
            pathlib.Path(__file__).parents[2]
            / "clients/admin-ui/src/features/privacycare"
        ).glob("*.slice.ts")
    ),
)

# Composed, not hardcoded: the prefix moved to /api/v1/... once the shipped
# UI's real path was checked, and a literal here would have silently
# stopped matching any route.
PRIVACYCARE_PATH_PREFIX = PRIVACYCARE_PREFIX

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
    # The business-process ROPA surface. These four have NO TypeScript
    # counterpart, and that is a fact about the product rather than an
    # omission: Fides has no business-process concept, so the shipped admin UI
    # ships no screen that could call these routes. The surface exists for the
    # consultant workflow the SOW describes ("Business Process Assessment and
    # Data Mapping"), for export, and for the phase-3 projection onto
    # process_node. If a UI is ever built for it, these entries come out and
    # real parity tests go in.
    "BusinessProcessResponse": {
        "ts_name": None,
        "reason": (
            "No TS counterpart: Fides has no business-process entity, so no "
            "shipped admin-UI screen calls this route. Carol's method and the "
            "SOW start from a business process; Fides' map starts from a "
            "system. See api/processes.py's module docstring."
        ),
    },
    "RopaDeclarationResponse": {
        "ts_name": None,
        "reason": (
            "No TS counterpart, same reason as BusinessProcessResponse. Note "
            "Fides DOES ship a ROPA CSV export, but it is driven by the "
            "plus/data-purpose/* family against a different, purpose-centric "
            "model — not this one."
        ),
    },
    "RopaEntryResponse": {
        "ts_name": None,
        "reason": (
            "No TS counterpart, same reason as BusinessProcessResponse."
        ),
    },
    "Page[BusinessProcessResponse]": {
        "ts_name": None,
        "reason": (
            "Generic pagination wrapper over a model that itself has no TS "
            "counterpart; there is nothing to be in parity with."
        ),
    },
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
    "DeletePrivacyAssessmentResponse": {
        "ts_name": None,
        "reason": (
            "Task 4: deletePrivacyAssessment types its RTK Query mutation "
            "as build.mutation<void, string> (privacy-assessments.slice.ts) "
            "— the admin-UI reads nothing from the DELETE response body, "
            "only invalidatesTags. This model exists solely to satisfy "
            "test_every_route_has_a_response_model_declared_or_inferred, "
            "which requires a non-None response_model on every "
            "plus/privacy-assessments route regardless of HTTP method."
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
    "Page[AssessmentTaskResponse]": {
        "ts_name": None,
        "reason": (
            "Same precedent as Page[TemplateResponse] above: "
            "fastapi_pagination.Page[T]'s generic envelope (items/total/"
            "page/size/pages) is proven once against the TS pagination "
            "contract by test_grouped_assessments_response_matches_the_"
            "shipped_contract — not re-asserted per T, even though a "
            "generated Page_AssessmentTaskResponse_.ts also happens to "
            "exist and mirrors that same shape field-for-field. "
            "AssessmentTaskResponse (T itself) is fully recursed into and "
            "carries its own parity tests in test_api_schemas.py."
        ),
    },
    # The Kenyan processing-grounds surface (D-KT-5) is deliberately NOT
    # here. Its three response models were allowlisted with the reason "the
    # hook in Task 6 defines its own type" — Task 6 landed in the same
    # branch, so the reason was false the day it was written, and with the
    # single-file FEATURE_TS_PATH above the entries could not have come out
    # even then. Both halves are fixed: the hand-authored interfaces in
    # features/privacycare/processing-grounds.slice.ts carry the same names
    # as the models, this walk now reads that file, and test_api_schemas.py
    # compares them field by field.
    "bytes": {
        "ts_name": None,
        "reason": (
            "Task 3 (config-and-pdf plan): GET .../{assessment_id}/pdf "
            "(api/reports.py) returns a raw PDF binary body via "
            "fastapi.Response(media_type='application/pdf'), not JSON. It "
            "declares response_model=bytes only so "
            "test_every_route_has_a_response_model_declared_or_inferred "
            "(test_api_registration.py) still passes — same precedent as "
            "DeletePrivacyAssessmentResponse above. Returning a Response "
            "instance directly bypasses response_model serialization "
            "entirely, so `bytes` is never actually validated against and "
            "has no TypeScript counterpart to be in parity with. (In "
            "practice `bytes` is not even a BaseModel subclass, so "
            "_discover_models never adds it to the walk at all — this "
            "entry exists for the same documentation reason as the others, "
            "not because a test would otherwise fail without it.)"
        ),
    },
    # The six discovery-monitor routes (plan 10, task 3). Controller ruling
    # for this task named exactly two required entries (MonitorConfigResponse
    # and MonitorExecutionResponse, both below) on the premise that "every
    # other model has a 1:1 TypeScript counterpart". Running this gate with
    # PRIVACYCARE_MONITORS_PREFIX actually armed (immediately above) showed
    # three more failures the ruling did not anticipate — Page[MonitorStatusResponse],
    # Page[str], and DeleteMonitorResponse — each of which is the SAME
    # pre-existing category this file already allowlists elsewhere, not a new
    # kind of gap:
    "MonitorConfigResponse": {
        "ts_name": "MonitorConfig",
        "reason": (
            "MonitorConfig is already a SQLAlchemy model name "
            "(fides.api.models.detection_discovery.core.MonitorConfig); this "
            "Pydantic response schema is suffixed *Response to avoid that "
            "collision, but the TS interface it mirrors is the unsuffixed "
            "MonitorConfig.ts (see MonitorConfigResponse's own docstring in "
            "monitor_schemas.py). Same precedent as AssessmentQuestionResponse "
            "-> AssessmentQuestion above."
        ),
    },
    "MonitorExecutionResponse": {
        "ts_name": "MonitorExecution",
        "reason": (
            "MonitorExecution is already a SQLAlchemy model name "
            "(fides.api.models.detection_discovery.core.MonitorExecution); "
            "same *Response-suffix-avoids-a-collision reason as "
            "MonitorConfigResponse immediately above, mirroring the unsuffixed "
            "MonitorExecution.ts."
        ),
    },
    "Page[MonitorStatusResponse]": {
        "ts_name": None,
        "reason": (
            "Same generic-pagination-wrapper precedent as Page[TemplateResponse] "
            "and Page[AssessmentTaskResponse] above: fastapi_pagination.Page[T]'s "
            "own shape is proven once, not re-asserted per T, even though a "
            "generated Page_MonitorStatusResponse_.ts also happens to exist (its "
            "underscored name is not what Page[T]'s own __name__ produces, so it "
            "would not be found by name anyway). MonitorStatusResponse (T itself) "
            "is fully recursed into and carries its own parity test in "
            "test_monitor_schemas.py."
        ),
    },
    "Page[str]": {
        "ts_name": None,
        "reason": (
            "Same generic-pagination-wrapper precedent as Page[MonitorStatusResponse] "
            "above. The leaf T=str is a bare scalar, not a BaseModel, so "
            "_discover_models never walks into it separately (same as `bytes` "
            "below) — there is no second entry to add for it, and no fields to "
            "drift: get_monitor_databases returns schema/database NAMES, a plain "
            "list of strings, matching the generated Page_str_.ts each caller "
            "already gets its shape from."
        ),
    },
    "DeleteMonitorResponse": {
        "ts_name": None,
        "reason": (
            "Same precedent as DeletePrivacyAssessmentResponse above: "
            "deleteDiscoveryMonitor types its RTK Query mutation as "
            "build.mutation<{ count: number }, ...> "
            "(discovery-detection.slice.ts) — an inline anonymous TS type "
            "literal, not a named interface or a generated model, so there is "
            "no name for `count: int` to be checked against. Unlike "
            "DeletePrivacyAssessmentResponse's case, the admin UI DOES read "
            "the field (`{ count }`, per delete_monitor's own docstring) — it "
            "simply never gave that shape a name to import."
        ),
    },
    # The DSR register's HTTP surface (plan 14, task 4). Same pattern as the
    # business-process ROPA surface above: the register is new, Kenyan-
    # specific ground with no Plus analogue, so no shipped admin-UI screen
    # calls any of these five routes and neither response model has a TS
    # counterpart to be in parity with.
    "DsrRequestResponse": {
        "ts_name": None,
        "reason": (
            "No TS counterpart: the DSR register is PrivacyCare's own "
            "(Barbara's 2026-09-15 ruling, spec D-DSR-1), and nothing in the "
            "shipped admin UI has a screen for it — the Privacy Center is "
            "the intake, per api/dsr.py's module docstring, and a bespoke "
            "register UI is explicitly out of this plan."
        ),
    },
    "Page[DsrRequestResponse]": {
        "ts_name": None,
        "reason": (
            "Generic pagination wrapper over a model that itself has no TS "
            "counterpart, same as Page[BusinessProcessResponse] above; "
            "there is nothing to be in parity with."
        ),
    },
}


# Every router this module registers. Listing them rather than filtering on a
# single prefix is deliberate: the business-process ROPA surface lives under
# its own namespace (/api/v1/privacycare/...) because no shipped UI calls it,
# and a walk anchored on the assessments prefix alone would have skipped that
# whole router in silence — a passing test measuring nothing, which is exactly
# the failure this file exists to catch.
#
# PRIVACYCARE_CHAT_PREFIX (task 3, the questionnaire chat's own router) is the
# THIRD router added to this app, and the second time one was added without
# this tuple being updated to match — the walk silently covered nothing for
# the whole plan (fix round for the processes router) until someone noticed.
# Do not let a fourth router repeat it: if `register()` in api/router.py ever
# gains another `app_setup.ROUTERS.append(...)`, its prefix belongs here too.
#
# PRIVACYCARE_MONITORS_PREFIX (plan 10, task 3): exactly that fourth router.
# Confirmed by running this file BEFORE adding it below: every test here
# still passed, silently checking nothing about the six new
# /plus/discovery-monitor* routes' response models — the same
# passes-while-measuring-nothing failure this comment already warns about.
#
# PRIVACYCARE_DSR_PREFIX (plan 14, task 4): a sixth router
# (privacycare_dsr_router). Its prefix, /api/v1/privacycare/dsr-requests,
# happens to already start with PRIVACYCARE_GROUNDS_PREFIX
# (/api/v1/privacycare), so the walk found its routes even before this line
# was added — listed explicitly anyway, both to follow the rule above for
# real (a future router might not share a prefix by accident) and because a
# reader should not have to notice the accidental substring match to know
# DSR is covered.
PRIVACYCARE_PATH_PREFIXES = (
    PRIVACYCARE_PREFIX,
    PRIVACYCARE_PROCESSES_PREFIX,
    PRIVACYCARE_CHAT_PREFIX,
    PRIVACYCARE_GROUNDS_PREFIX,
    PRIVACYCARE_MONITORS_PREFIX,
    PRIVACYCARE_DSR_PREFIX,
)


def _privacycare_response_models():
    return [
        r.response_model
        for r in app.routes
        if getattr(r, "path", "").startswith(PRIVACYCARE_PATH_PREFIXES)
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
    return any(
        re.search(rf"export interface {re.escape(name)}\b", path.read_text())
        for path in FEATURE_TS_PATH
    )


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
                f"`export interface {ts_name}` in any of "
                f"{[path.name for path in FEATURE_TS_PATH]} — "
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
