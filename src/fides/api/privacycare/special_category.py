# Whether a declaration is special-category under DPA 2019, derived from the
# taxonomy rather than typed in twice.
#
# The declarant ticks `processes_special_category_data` by hand on the
# declaration form. Separately, the Kenyan taxonomy loader
# (taxonomy/loader.py) tags certain ctl_data_categories rows (and their
# descendants, by construction — see D-KT-2 / test_ancestor_tag_is_inherited)
# with kenyan.SPECIAL_TAG. This module answers "does the tag agree with the
# tick?" without trusting either source to have done the other's job: a
# declared category can be a LEAF under a tagged ancestor (e.g.
# `user.biometric.fingerprint` under the tagged `user.biometric`), so the
# match has to walk ancestors, not just the declared key itself.
#
# `mismatch` is deliberately one-directional: declared=True, derived=False is
# not a finding — the declarant may know something the taxonomy does not
# (D-KT rule, see SpecialCategoryView docstring). Only declared=False,
# derived=True is worth surfacing, because that is the taxonomy telling us
# the declarant's "no" undercounts the estate.
from dataclasses import dataclass
from typing import Optional

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.taxonomy.kenyan import SPECIAL_TAG

_DECLARATION_FLAG_SQL = sqlalchemy.text(
    "SELECT processes_special_category_data FROM privacydeclaration WHERE id = :declaration_id"
)

# Unnest the declaration's own data_categories, then for each declared key
# generate every ancestor prefix of its dotted path (including the key
# itself) by splitting on '.' and re-joining increasing-length slices —
# `user.biometric.fingerprint` yields `user`, `user.biometric`,
# `user.biometric.fingerprint`. A declared key matches if ANY of its
# prefixes carries the tag in ctl_data_categories.tags. The result is the
# DISTINCT declared keys that matched (not the ancestor keys that carried
# the tag), sorted for a deterministic triggering_keys list.
_TRIGGERING_KEYS_SQL = sqlalchemy.text(
    """
    WITH declared_keys AS (
        SELECT unnest(d.data_categories) AS declared_key
        FROM privacydeclaration d
        WHERE d.id = :declaration_id
    ),
    prefixes AS (
        SELECT dk.declared_key,
               array_to_string(
                   (string_to_array(dk.declared_key, '.'))[1:n], '.'
               ) AS ancestor_key
        FROM declared_keys dk,
             generate_series(
                 1, array_length(string_to_array(dk.declared_key, '.'), 1)
             ) AS n
    )
    SELECT DISTINCT p.declared_key
    FROM prefixes p
    JOIN ctl_data_categories cc ON cc.fides_key = p.ancestor_key
    WHERE :tag = ANY(cc.tags)
    ORDER BY p.declared_key
    """
)


@dataclass(frozen=True)
class SpecialCategoryView:
    declared: Optional[bool]  # processes_special_category_data as stored; None if NULL
    derived: bool  # any declared category (or an ancestor of it) carries SPECIAL_TAG
    triggering_keys: list[str]  # the DECLARED keys that matched, distinct and sorted
    mismatch: bool  # declared is False and derived is True — a finding


def derive_special_category(db: Session, declaration_id: str) -> SpecialCategoryView:
    """Read-only. Raises LookupError if declaration_id names no
    privacydeclaration row — callers must not receive a fabricated view for
    an id that does not exist."""
    row = db.execute(
        _DECLARATION_FLAG_SQL, {"declaration_id": declaration_id}
    ).mappings().first()
    if row is None:
        raise LookupError(f"no privacy declaration with id {declaration_id!r}")

    declared = row["processes_special_category_data"]

    # DISTINCT + ORDER BY p.declared_key in the query above already makes
    # this list distinct and sorted (M4: a Python sorted() here sorted the
    # same list a second time). The ordering guarantee lives in the SQL, and
    # SpecialCategoryView.triggering_keys documents it.
    triggering_keys = [
        r["declared_key"]
        for r in db.execute(
            _TRIGGERING_KEYS_SQL, {"declaration_id": declaration_id, "tag": SPECIAL_TAG}
        ).mappings().all()
    ]

    return SpecialCategoryView(
        declared=declared,
        derived=bool(triggering_keys),
        triggering_keys=triggering_keys,
        mismatch=declared is False and bool(triggering_keys),
    )
