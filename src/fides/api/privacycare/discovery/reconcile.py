"""Reconcile a scan against what is already staged.

SPEC D-EX-3 / D-EX-5 / D-EX-6. Task 1's `walk_catalogue` only ever reads;
this module is the first thing in the discovery pipeline that writes. It
takes the flat list of `FoundResource` the walk returned, plus the current
session, and turns the difference between "found this run" and "staged
already" into `stagedresource` rows and `stagedresourceancestor` edges.

D-EX-5, the decision this module exists to enforce: a resource that
disappeared from the source is never DELETEd from `stagedresource`. It is
marked `diff_status = 'removal'` and stays -- a controller must be able to
show a table held personal data last quarter even though this quarter's
scan no longer finds it, and a DELETE would destroy exactly that evidence.
There is no DELETE statement anywhere below.

D-EX-6: a re-scan that finds the same resources again must not touch their
existing row at all -- not even to "confirm" it -- so a consultant can
re-run discovery as often as they like without generating diff noise. Only
three things ever change a stored row here: (1) it is new, so it gets
INSERTed as 'addition'; (2) it went missing from this scan and its
monitor's row gets UPDATEd to 'removal'; or (3) it was marked 'removal' by
an earlier scan and is back, so it gets UPDATEd back to 'addition' (see
Resurrection below). Everything else is left alone.

Resurrection: a urn that was staged, went missing (diff_status='removal'),
and is found again is not "unchanged" -- unchanged would silently assert
that nothing happened to it, which is false: it vanished and came back
between two scans. It is treated as an addition relative to the last
observed state (counted in `added`, not `unchanged`) precisely so a
reviewer looks at it again rather than have it slide back into the
furniture unnoticed. Left un-handled, a resurrected urn would stay on
`removal` forever -- D-EX-5 exists to stop us destroying evidence that a
resource *was* there; this exists to stop us asserting a resource is gone
when this very scan proves it is not.

Scoping (the controller's ruling on this task): every query below that
decides what has gone, what already exists, or what has come back is
filtered on `monitor_config_id`. Without that filter, the FIRST run of a
brand-new monitor would compare its (small) found list against *every*
stagedresource row from *every* monitor and mark them all `removal` -- a
fleet of false "this table disappeared" findings, for tables the new
monitor never looked at.
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.detection_discovery.core import (
    DiffStatus,
    StagedResourceAncestor,
    StagedResourceType,
)
from fides.api.privacycare.discovery.walk import FoundResource

# F7: Ethyca's own convention for a column's SQL type is meta->>'data_type'
# (see backfill_stagedresource_is_leaf.py's own is_leaf formula, which reads
# this same key). F8: a view/materialised view is staged as a Table (see
# walk.py's FoundResource.table_type docstring) with this key naming what it
# really is; an ordinary table leaves meta.table_type unset.
_META_DATA_TYPE_KEY = "data_type"
_META_TABLE_TYPE_KEY = "table_type"
# F5: stashed onto a row the moment it is marked 'removal' so resurrection
# can restore the status it actually had -- see _MARK_REMOVED_SQL /
# _RESURRECT_SQL and the module docstring's Resurrection paragraph below.
_META_PRE_REMOVAL_STATUS_KEY = "pre_removal_diff_status"


def _meta_for(resource: FoundResource) -> Dict[str, Any]:
    """What `_INSERT_SQL` writes into a brand-new row's `meta`. Field rows
    carry their SQL type (F7); Table rows carry `table_type` only when the
    walk tagged them as a view or materialised view (F8) -- an ordinary
    table's meta stays `{}`, unchanged from before this fix."""
    if (
        resource.resource_type == StagedResourceType.FIELD.value
        and resource.field_type is not None
    ):
        return {_META_DATA_TYPE_KEY: resource.field_type}
    if (
        resource.resource_type == StagedResourceType.TABLE.value
        and resource.table_type is not None
    ):
        return {_META_TABLE_TYPE_KEY: resource.table_type}
    return {}


# `StagedResource.id`'s ORM default (FidesBase.generate_uuid) derives its
# prefix from the table name at flush time, but that default only fires for
# objects created through the ORM -- this module writes raw SQL instead (the
# plan's constraint), so the prefix has to be spelled out. "sta_" matches
# what generate_uuid would have produced for this table
# ("stagedresource"[:3]), the same way core.py's own
# create_all_staged_resource_ancestor_links hand-spells "srl_" for
# stagedresourceancestor rather than relying on the ORM default.
#
# Scoped by monitor_config_id even though `urn` is already globally unique
# (walk.py's D-EX-2 contract puts monitor_key in every urn's first
# segment), so this query's correctness does not rest on a contract owned
# by a different module. Selects diff_status too, not just urn -- that is
# what lets reconcile() tell an unchanged match from a resurrected one
# (see the module docstring's Resurrection paragraph) without a second
# round-trip.
_FIND_EXISTING_SQL = sqlalchemy.text(
    """
    SELECT urn, diff_status FROM stagedresource
    WHERE monitor_config_id = :monitor_config_id
      AND urn = ANY(CAST(:urns AS text[]))
    """
)

# F3: two overlapping runs (a double-clicked Scan, or a retry racing a still
# -running execution) compute the same `new_resources` from the same
# pre-state and both try to INSERT the same urn; `ix_stagedresource_urn` is
# a UNIQUE index, so without ON CONFLICT the loser's entire transaction
# aborts -- not just that one row, every write reconcile() made in the same
# session. "DO NOTHING" makes the loser's redundant insert a no-op instead,
# and RETURNING urn lets the caller count only the rows THIS call actually
# inserted (a race's loser correctly reports fewer `added` than urns it
# attempted). F6/F7/F8: is_leaf and meta are now written on insert instead
# of left at their bare defaults -- see `_meta_for` above and the module's
# is_leaf paragraph below.
_INSERT_SQL = sqlalchemy.text(
    """
    INSERT INTO stagedresource (
        id, urn, name, resource_type, parent, monitor_config_id, diff_status,
        classifications, user_assigned_data_categories, children, meta,
        is_leaf
    ) VALUES (
        'sta_' || gen_random_uuid(), :urn, :name, :resource_type, :parent,
        :monitor_config_id, :diff_status, '{}', '{}', '{}',
        CAST(:meta AS jsonb), :is_leaf
    )
    ON CONFLICT (urn) DO NOTHING
    RETURNING urn
    """
)

# Scoped by monitor_config_id -- see the module docstring's Scoping
# paragraph. "IS DISTINCT FROM :removal" rather than "<> :removal" so a row
# whose diff_status happens to be NULL is still caught, and so re-running
# this against an already-removed row RETURNs nothing -- reconcile()'s
# `removed` count is "newly found gone this call", not "gone in total".
#
# F5: `meta = meta || jsonb_build_object(...)` stashes the status the row
# actually had a moment before this UPDATE overwrites it -- the right-hand
# side of a Postgres UPDATE's SET clause always reads the pre-update row, so
# `diff_status` there is still the OLD value even though the same statement
# is about to change the column of that name. Without this, a resurrected
# row (see _RESURRECT_SQL below) has no way to know it was ever anything
# but 'addition' -- a promoted ('monitored') or muted resource that misses
# one scan would resurrect as brand-new work, its status silently gone.
_MARK_REMOVED_SQL = sqlalchemy.text(
    """
    UPDATE stagedresource
    SET diff_status = :removal,
        meta = meta || jsonb_build_object(:pre_removal_key, diff_status)
    WHERE monitor_config_id = :monitor_config_id
      AND urn <> ALL(CAST(:found_urns AS text[]))
      AND diff_status IS DISTINCT FROM :removal
    RETURNING urn
    """
)

# See the module docstring's Resurrection paragraph. Scoped by
# monitor_config_id for the same reason as every other query here that
# decides a urn's fate, though the urn list itself is already known-exact
# (computed in Python from _FIND_EXISTING_SQL's own result), so this is a
# second scoping belt on top of a first, not the only one.
#
# F5: restores whatever _MARK_REMOVED_SQL stashed rather than always
# resetting to 'addition' -- COALESCE falls back to :addition only when
# there is nothing stashed (a row marked 'removal' by code that predates
# this fix, or -- defensively -- any other gap). The stashed key is then
# deleted from meta (`meta - :pre_removal_key`) so a SECOND removal/
# resurrection cycle stashes fresh, rather than a stale key silently
# surviving underneath a new one written on top of it.
_RESURRECT_SQL = sqlalchemy.text(
    """
    UPDATE stagedresource
    SET diff_status = COALESCE(meta->>:pre_removal_key, :addition),
        meta = meta - :pre_removal_key
    WHERE monitor_config_id = :monitor_config_id
      AND urn = ANY(CAST(:urns AS text[]))
    """
)


@dataclass(frozen=True)
class ReconcileSummary:
    """The result of one reconcile() call. All three fields are counts of
    what changed THIS call, not running totals -- `removed`, in particular,
    is "urns newly marked removal by this call", not "urns currently
    removal under this monitor" (a urn already sitting on `removal` from an
    earlier call is left untouched and is not recounted). `added` includes
    both brand-new urns and resurrected ones -- see reconcile.py's module
    docstring, Resurrection.
    """

    added: int
    removed: int
    unchanged: int


def reconcile(
    db: Session,
    *,
    monitor_config_id: str,
    monitor_key: str,
    found: List[FoundResource],
) -> ReconcileSummary:
    """Diff `found` (this scan) against `stagedresource` (the last scan) and
    write only the difference. Never commits -- the caller's session
    boundary decides, the same as every other PrivacyCare core module.

    `monitor_key` is not written anywhere below: every `FoundResource.urn`
    already carries it as the URN's first segment (walk.py's D-EX-2
    contract), so it is already implicit in every urn this function sees.
    It stays a required, explicit keyword here anyway so a caller cannot
    reconcile one monitor's walk output while accidentally attributing it
    to a different monitor's identity by only getting monitor_config_id
    right.
    """
    found_urns = [resource.urn for resource in found]

    # diff_status per already-staged urn (scoped to this monitor) is what
    # separates three cases below: absent entirely (new), present but on
    # 'removal' (resurrected), present and anything else (unchanged, and
    # therefore untouched -- D-EX-6).
    existing_status: Dict[str, Optional[str]] = {
        row.urn: row.diff_status
        for row in db.execute(
            _FIND_EXISTING_SQL,
            {"monitor_config_id": monitor_config_id, "urns": found_urns},
        )
    }

    new_resources = [
        resource for resource in found if resource.urn not in existing_status
    ]
    resurrected_urns = [
        resource.urn
        for resource in found
        if existing_status.get(resource.urn) == DiffStatus.REMOVAL.value
    ]

    # F3: `inserted_count` -- not `len(new_resources)` -- is what actually
    # landed. Under a race (a concurrent run's reconcile() already inserted
    # the same urn between _FIND_EXISTING_SQL above and this INSERT),
    # ON CONFLICT DO NOTHING makes that one row a no-op rather than
    # aborting the whole batch, and this call's `added` count correctly
    # reports only what IT inserted. `RETURNING` rows are not fetchable
    # from a multi-row (executemany-style) statement in SQLAlchemy -- the
    # DBAPI cursor closes without them -- but `rowcount` still reflects the
    # true number of rows this statement actually inserted (verified
    # directly: a 3-row batch with one pre-existing conflicting id reports
    # rowcount == 2), so that is what is counted rather than the length of
    # the attempted batch.
    inserted_count = 0
    if new_resources:
        result = db.execute(
            _INSERT_SQL,
            [
                {
                    "urn": resource.urn,
                    "name": resource.name,
                    "resource_type": resource.resource_type,
                    "parent": resource.parent_urn,
                    "monitor_config_id": monitor_config_id,
                    "diff_status": DiffStatus.ADDITION.value,
                    "meta": json.dumps(_meta_for(resource)),
                    "is_leaf": resource.resource_type == StagedResourceType.FIELD.value,
                }
                for resource in new_resources
            ],
        )
        inserted_count = result.rowcount

    if resurrected_urns:
        db.execute(
            _RESURRECT_SQL,
            {
                "monitor_config_id": monitor_config_id,
                "urns": resurrected_urns,
                "addition": DiffStatus.ADDITION.value,
                "pre_removal_key": _META_PRE_REMOVAL_STATUS_KEY,
            },
        )

    removed_urns = (
        db.execute(
            _MARK_REMOVED_SQL,
            {
                "monitor_config_id": monitor_config_id,
                "found_urns": found_urns,
                "removal": DiffStatus.REMOVAL.value,
                "pre_removal_key": _META_PRE_REMOVAL_STATUS_KEY,
            },
        )
        .scalars()
        .all()
    )

    _write_ancestry(db, found)

    added = inserted_count + len(resurrected_urns)
    return ReconcileSummary(
        added=added,
        removed=len(removed_urns),
        unchanged=len(found) - added,
    )


def _write_ancestry(db: Session, found: List[FoundResource]) -> None:
    """Every found resource's ancestor chain, walked from `found` itself.

    A real `walk_catalogue` call always emits a resource's whole ancestor
    chain in the same batch (it traverses database -> schema -> table ->
    field top-down), so resolving "who is my parent" only ever needs to
    look inside `found` -- no extra DB round-trip. If a resource's
    parent_urn is not present in this batch (only possible when a caller
    hands reconcile() a partial slice, as some of this module's own tests
    deliberately do to isolate one behaviour), its chain simply stops there
    instead of guessing: distances for ancestors this call cannot see are
    left unwritten, not written wrong.

    Idempotent by construction: StagedResourceAncestor's own bulk-insert
    helper does `ON CONFLICT (ancestor_urn, descendant_urn) DO NOTHING`, so
    reconciling the same batch twice does not duplicate an edge.
    """
    by_urn: Dict[str, FoundResource] = {resource.urn: resource for resource in found}
    links: Dict[str, Set[Tuple[str, int]]] = {}

    for resource in found:
        ancestors: Set[Tuple[str, int]] = set()
        distance = 1
        parent_urn = resource.parent_urn
        while parent_urn is not None:
            parent = by_urn.get(parent_urn)
            if parent is None:
                break
            ancestors.add((parent.urn, distance))
            distance += 1
            parent_urn = parent.parent_urn
        if ancestors:
            links[resource.urn] = ancestors

    if links:
        StagedResourceAncestor.create_all_staged_resource_ancestor_links(db, links)
