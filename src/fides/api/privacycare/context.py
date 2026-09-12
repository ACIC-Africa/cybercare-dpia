"""What the generator knows before it writes anything.

Two reads, both pure: which (system, declaration) pairs to assess, and what
facts Fides already holds about each. Kept out of generator.py so the
coverage policy can be tested against a hand-built dict with no database.

No ORM coupling — raw SQL via sqlalchemy.text(), the same convention
assessments.py and answers.py follow.
"""
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy.orm import Session

# fides_sources addresses nine roots; build_context supplies four (system,
# privacy_declaration, data_use, data_category). The five below are
# deliberately unsupported in phase 1 — they name Fides subsystems
# PrivacyCare does not operate (consent notices and experiences, DSR
# policies, integrations, Fides' own config), so there is no data to resolve
# them against. resolve_source returns None for them, which routes the
# question to a human exactly as a `none`-coverage question is routed.
# Listed here so the gap is a recorded decision and not an oversight, and so
# that adding one later is an obvious edit.
UNSUPPORTED_SOURCE_ROOTS = frozenset(
    {"privacy_notice", "privacy_experience", "policy", "connection", "fides"}
)

# The roots build_context actually populates. Its counterpart,
# UNSUPPORTED_SOURCE_ROOTS, records the ones phase 1 deliberately cannot
# resolve; together they must account for every root the shipped templates
# cite. Named so that pairing can be checked against the live templates rather
# than against itself — the earlier check restated the unsupported set back to
# itself, so a tenth root added upstream would have fallen through both.
SUPPLIED_SOURCE_ROOTS = frozenset(
    {"system", "privacy_declaration", "data_use", "data_category"}
)


@dataclass(frozen=True)
class GenerationTarget:
    """One assessment's worth of subject: a privacy declaration in a system.

    The unit is the DECLARATION, not the system. privacy_assessment carries
    declaration_id, declaration_name, data_use, data_use_name and
    data_categories — all declaration-level — and Ethyca's own model
    docstring says a task "may produce multiple PrivacyAssessment records
    (one per system/declaration)". A system with three declarations yields
    three assessments per requested type.
    """

    system_fides_key: str
    system_name: str | None
    declaration_id: str
    declaration_name: str | None
    data_use: str
    data_use_name: str | None
    data_categories: list[str]


# The ORDER BY is not cosmetic: it makes a run's assessment ids, and so the
# task's assessment_ids list, deterministic for a given estate. A
# nondeterministic order would make two runs over the same data
# indistinguishable-but-different in an audit.
_SELECT_TARGETS_SQL = """
    SELECT s.fides_key AS system_fides_key,
           s.name      AS system_name,
           d.id        AS declaration_id,
           d.name      AS declaration_name,
           d.data_use  AS data_use,
           du.name     AS data_use_name,
           d.data_categories AS data_categories
    FROM privacydeclaration d
    JOIN ctl_systems s ON s.id = d.system_id
    LEFT JOIN ctl_data_uses du ON du.fides_key = d.data_use
    WHERE TRUE
"""


def select_targets(
    db: Session,
    system_fides_keys: list[str] | None,
    high_risk_only: bool,
) -> list[GenerationTarget]:
    """Every declaration to assess, in a deterministic order.

    system_fides_keys=None means "every system" — the request contract says
    so explicitly ("None = all systems"). An EMPTY list is the opposite
    request and returns nothing: `= ANY(ARRAY[])` matches no row. The two
    must not be conflated anywhere upstream; see _create_task in
    api/tasks.py for the place they once were.

    high_risk_only filters on ctl_systems.requires_data_protection_assessments
    and nothing else. See OQ-PRIVACY-10: that column's name states its own
    meaning, so reading it is not a legal judgement, whereas deciding that
    uses_profiling or processes_special_category_data constitutes a Kenyan
    DPA 2019 trigger is one — and that determination is Carol's, not
    engineering's. This predicate is deliberately a single WHERE clause in a
    single function so broadening it later is a one-place change.
    """
    sql = _SELECT_TARGETS_SQL
    params: dict = {}
    if system_fides_keys is not None:
        sql += " AND s.fides_key = ANY(:system_fides_keys)"
        params["system_fides_keys"] = list(system_fides_keys)
    if high_risk_only:
        sql += " AND s.requires_data_protection_assessments"
    sql += " ORDER BY s.fides_key, d.data_use, d.id"

    rows = db.execute(sqlalchemy.text(sql), params).mappings().all()
    return [
        GenerationTarget(
            system_fides_key=row["system_fides_key"],
            system_name=row["system_name"],
            declaration_id=row["declaration_id"],
            declaration_name=row["declaration_name"],
            data_use=row["data_use"],
            data_use_name=row["data_use_name"],
            data_categories=list(row["data_categories"] or []),
        )
        for row in rows
    ]


_SYSTEM_DETAIL_SQL = sqlalchemy.text(
    "SELECT name, description, privacy_policy, dpo, data_security_practices, "
    "       uses_profiling, does_international_transfers, "
    "       requires_data_protection_assessments "
    "FROM ctl_systems WHERE fides_key = :fides_key"
)

_DECLARATION_DETAIL_SQL = sqlalchemy.text(
    "SELECT name, data_use, data_categories, data_subjects, "
    "       legal_basis_for_processing, retention_period, "
    "       processes_special_category_data, special_category_legal_basis, "
    "       data_shared_with_third_parties, third_parties, shared_categories "
    "FROM privacydeclaration WHERE id = :declaration_id"
)

_DATA_USE_DETAIL_SQL = sqlalchemy.text(
    "SELECT name, description FROM ctl_data_uses WHERE fides_key = :fides_key"
)

_DATA_CATEGORY_DETAIL_SQL = sqlalchemy.text(
    "SELECT fides_key, name, description FROM ctl_data_categories "
    "WHERE fides_key = ANY(:fides_keys)"
)


def build_context(db: Session, target: GenerationTarget) -> dict:
    """The facts available about one target, keyed the way fides_sources
    addresses them.

    The top-level keys are not arbitrary: assessment_question.fides_sources
    holds dotted paths whose first segment names one of nine roots (sampled
    live: ['system.name', 'system.description', 'privacy_declaration.name'],
    ['privacy_declaration.data_use', 'data_use.name',
    'data_use.description'], ['data_category.name']). This function supplies
    four of the nine — system, privacy_declaration, data_use, data_category
    — because those are the ones PrivacyCare has data for. The other five
    are named in UNSUPPORTED_SOURCE_ROOTS and never appear here: see that
    constant's docstring for why, and unresolvable_roots() for how a caller
    finds out which roots a given question's sources actually missed.

    A data_use with no ctl_data_uses row yields None name/description rather
    than raising — a declaration may legitimately name a custom data use,
    and one unregistered taxonomy key must not stop the whole system's
    generation. data_category does the same per-category: a category key
    with no ctl_data_categories row falls back to the key itself for
    "name" (the key is still a true statement about the processing, unlike
    an invented label) and to None for "description" (there is nothing
    true to say).

    The returned dict is stored verbatim in privacy_assessment.
    context_snapshot, so it must contain only JSON-serialisable values. That
    snapshot is what lets a DPO answer "which facts produced this answer"
    years later — the same reason the answer chain is append-only.
    """
    system = db.execute(
        _SYSTEM_DETAIL_SQL, {"fides_key": target.system_fides_key}
    ).mappings().first()
    declaration = db.execute(
        _DECLARATION_DETAIL_SQL, {"declaration_id": target.declaration_id}
    ).mappings().first()
    data_use = db.execute(
        _DATA_USE_DETAIL_SQL, {"fides_key": target.data_use}
    ).mappings().first()

    category_keys = list(target.data_categories or [])
    category_rows_by_key: dict = {}
    if category_keys:
        category_rows = db.execute(
            _DATA_CATEGORY_DETAIL_SQL, {"fides_keys": category_keys}
        ).mappings().all()
        category_rows_by_key = {row["fides_key"]: row for row in category_rows}
    # Declaration order, not query order — a declaration has many
    # categories, fides_sources addresses the path singular, and the
    # taxonomy join must not silently reorder what the customer declared.
    category_names = [
        (category_rows_by_key[key]["name"] or key)
        if key in category_rows_by_key
        else key
        for key in category_keys
    ]
    category_descriptions = [
        category_rows_by_key[key]["description"] if key in category_rows_by_key else None
        for key in category_keys
    ]

    return {
        "system": {
            "fides_key": target.system_fides_key,
            **{k: _jsonable(v) for k, v in dict(system or {}).items()},
        },
        "privacy_declaration": {
            "id": target.declaration_id,
            **{k: _jsonable(v) for k, v in dict(declaration or {}).items()},
        },
        "data_use": {
            "fides_key": target.data_use,
            "name": data_use["name"] if data_use else None,
            "description": data_use["description"] if data_use else None,
        },
        "data_category": {
            "name": category_names,
            "description": category_descriptions,
        },
    }


def _jsonable(value):
    """Postgres ARRAY columns come back as lists already; everything else
    here is a scalar. Normalise None-vs-empty so context_snapshot never
    carries a driver-specific type."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def resolve_source(context: dict, dotted_path: str) -> str | None:
    """Render one fides_sources path as text, or None if it is not known.

    None means "this fact is absent" and is what the coverage policy keys
    off. An absent fact must never render as "None", "[]" or "" — a DPIA
    answer reading "Data categories: []" asserts something false about the
    customer's processing.

    A list renders comma-separated because every list-valued source here
    (data_categories, data_subjects, shared_categories) is a set of
    taxonomy keys a reader wants inline, not JSON.
    """
    parts = dotted_path.split(".", 1)
    if len(parts) != 2:
        return None
    root, field = parts
    section = context.get(root)
    if not isinstance(section, dict):
        return None
    value = section.get(field)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(str(item) for item in value if item is not None)
        return rendered or None
    if isinstance(value, bool):
        return "yes" if value else "no"
    rendered = str(value).strip()
    return rendered or None


def unresolvable_roots(context: dict, source_paths: list[str]) -> set[str]:
    """Which top-level roots, among source_paths, resolve_source could not
    answer for this context.

    A root lands in the result either because it is in
    UNSUPPORTED_SOURCE_ROOTS (a recorded phase-1 gap — no data exists for it
    regardless of context) or because at least one of its paths resolved to
    None in this specific context (a supported root that happens to have
    nothing to say here, e.g. a declaration with no data_categories).

    This makes the gap countable rather than silent: a caller can log, per
    assessment, which roots contributed nothing — instead of discovering
    from a customer that half a template came back blank. It deliberately
    reports at root granularity, matching how fides_sources addresses the
    schema, not at the level of individual paths.
    """
    unresolved: set[str] = set()
    for path in source_paths:
        root = path.split(".", 1)[0]
        if root in UNSUPPORTED_SOURCE_ROOTS:
            unresolved.add(root)
            continue
        if resolve_source(context, path) is None:
            unresolved.add(root)
    return unresolved
