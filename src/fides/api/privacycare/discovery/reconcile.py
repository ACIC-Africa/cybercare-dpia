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
two things ever change a stored row here: (1) it is new, so it gets
INSERTed as 'addition', or (2) it went missing from this scan and its
monitor's row gets UPDATEd to 'removal'. Everything else is left alone.

Scoping (the controller's ruling on this task): the "went missing" query
below is filtered on `monitor_config_id`. Without that filter, the FIRST
run of a brand-new monitor would compare its (small) found list against
*every* stagedresource row from *every* monitor and mark them all
`removal` -- a fleet of false "this table disappeared" findings, for
tables the new monitor never looked at. Every query in this module that
decides what has gone carries that filter.
"""
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.models.detection_discovery.core import (
    DiffStatus,
    StagedResourceAncestor,
)
from fides.api.privacycare.discovery.walk import FoundResource

# `StagedResource.id`'s ORM default (FidesBase.generate_uuid) derives its
# prefix from the table name at flush time, but that default only fires for
# objects created through the ORM -- this module writes raw SQL instead (the
# plan's constraint), so the prefix has to be spelled out. "sta_" matches
# what generate_uuid would have produced for this table
# ("stagedresource"[:3]), the same way core.py's own
# create_all_staged_resource_ancestor_links hand-spells "srl_" for
# stagedresourceancestor rather than relying on the ORM default.
_FIND_EXISTING_SQL = sqlalchemy.text(
    "SELECT urn FROM stagedresource WHERE urn = ANY(CAST(:urns AS text[]))"
)

_INSERT_SQL = sqlalchemy.text(
    """
    INSERT INTO stagedresource (
        id, urn, name, resource_type, parent, monitor_config_id, diff_status,
        classifications, user_assigned_data_categories, children, meta
    ) VALUES (
        'sta_' || gen_random_uuid(), :urn, :name, :resource_type, :parent,
        :monitor_config_id, :diff_status, '{}', '{}', '{}', '{}'::jsonb
    )
    """
)

# Scoped by monitor_config_id -- see the module docstring's Scoping
# paragraph. "IS DISTINCT FROM :removal" rather than "<> :removal" so a row
# whose diff_status happens to be NULL is still caught, and so re-running
# this against an already-removed row RETURNs nothing -- reconcile()'s
# `removed` count is "newly found gone this call", not "gone in total".
_MARK_REMOVED_SQL = sqlalchemy.text(
    """
    UPDATE stagedresource
    SET diff_status = :removal
    WHERE monitor_config_id = :monitor_config_id
      AND urn <> ALL(CAST(:found_urns AS text[]))
      AND diff_status IS DISTINCT FROM :removal
    RETURNING urn
    """
)


@dataclass(frozen=True)
class ReconcileSummary:
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

    existing_urns: Set[str] = set(
        db.execute(_FIND_EXISTING_SQL, {"urns": found_urns}).scalars().all()
    )
    new_resources = [resource for resource in found if resource.urn not in existing_urns]

    if new_resources:
        db.execute(
            _INSERT_SQL,
            [
                {
                    "urn": resource.urn,
                    "name": resource.name,
                    "resource_type": resource.resource_type,
                    "parent": resource.parent_urn,
                    "monitor_config_id": monitor_config_id,
                    "diff_status": DiffStatus.ADDITION.value,
                }
                for resource in new_resources
            ],
        )

    removed_urns = (
        db.execute(
            _MARK_REMOVED_SQL,
            {
                "monitor_config_id": monitor_config_id,
                "found_urns": found_urns,
                "removal": DiffStatus.REMOVAL.value,
            },
        )
        .scalars()
        .all()
    )

    _write_ancestry(db, found)

    return ReconcileSummary(
        added=len(new_resources),
        removed=len(removed_urns),
        unchanged=len(found) - len(new_resources),
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
