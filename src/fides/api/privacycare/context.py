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
    so explicitly ("None = all systems").

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


def build_context(db: Session, target: GenerationTarget) -> dict:
    """The facts available about one target, keyed the way fides_sources
    addresses them.

    The three top-level keys are not arbitrary: assessment_question.
    fides_sources holds dotted paths whose first segment is exactly
    "system", "privacy_declaration" or "data_use" (sampled live:
    ['system.name', 'system.description', 'privacy_declaration.name'],
    ['privacy_declaration.data_use', 'data_use.name',
    'data_use.description']). This dict IS that addressing space.

    A data_use with no ctl_data_uses row yields None name/description rather
    than raising — a declaration may legitimately name a custom data use,
    and one unregistered taxonomy key must not stop the whole system's
    generation.

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
