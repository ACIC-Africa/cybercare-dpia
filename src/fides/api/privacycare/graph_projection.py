# Projection of a ROPA entry onto Cybercare graph shapes.
#
# PrivacyCare does not write to the Cybercare graph — that is phase 3, and it
# goes through the bucket-1 live-feed contract (ADR-024). What this module
# guarantees is that when the feed is built, it is a projection and not a
# translation: every field has a destination, and the shapes are emitted here
# so a test can assert them without a Cybercare instance in the room.
#
# IMPORTANT: edge.source_id and edge.target_id are BATCH-LOCAL CORRELATION
# KEYS, not real Cybercare graph foreign keys. process_node.id and
# data_node.id do not exist until a loader INSERTs those rows and the target
# database assigns them. A phase-3 loader MUST insert the nodes first,
# capture the database-assigned ids, remap the edges from these correlation
# keys to the real assigned ids, and only then persist the edges. Inserting
# the edges emitted here verbatim, without that remap step, produces orphan
# edges or foreign-key violations.
from dataclasses import dataclass, field

from fides.api.privacycare.ropa import RopaEntry

PROVENANCE = "privacycare"


@dataclass
class GraphProjection:
    # process_node and each entry of data_nodes are schema-exact against the
    # target process_node / data_node shapes and carry no id field — real
    # ids are assigned by the target database on insert.
    #
    # node_correlation_ids holds the batch-local composite key for each
    # entry in data_nodes, in the same order and of the same length.
    # edges[i].target_id is one of these correlation keys, not a real graph
    # id. A loader must insert data_nodes, capture the assigned ids keyed by
    # node_correlation_ids, remap the edges accordingly, and only then
    # persist the edges — inserting edges as emitted here produces orphan
    # edges.
    process_node: dict
    data_nodes: list[dict] = field(default_factory=list)
    node_correlation_ids: list[str] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)


def project_to_graph(entry: RopaEntry, organisation_id: str) -> GraphProjection:
    # Map one ROPA entry onto process_node, data_node and
    # edge_process_handles_data shapes.
    #
    # One data_node per (declaration, data_category) pair: the graph's data_node
    # carries a single pii_category, while a declaration may name several.
    process_node = {
        "organisation_id": organisation_id,
        "name": entry.process.name,
        "description": getattr(entry.process, "description", None),
        "provenance": PROVENANCE,
    }

    data_nodes: list[dict] = []
    node_correlation_ids: list[str] = []
    edges: list[dict] = []
    for declaration in entry.declarations:
        for category in declaration.data_categories:
            node_id = f"{declaration.id}:{category}"
            data_nodes.append(
                {
                    "organisation_id": organisation_id,
                    "name": category,
                    "pii_category": category,
                    "lawful_basis": declaration.legal_basis,
                    "retention_policy": declaration.retention_period,
                    "provenance": PROVENANCE,
                }
            )
            node_correlation_ids.append(node_id)
            edges.append(
                {
                    "organisation_id": organisation_id,
                    "source_id": entry.process.id,
                    "target_id": node_id,
                    "provenance": PROVENANCE,
                }
            )

    return GraphProjection(
        process_node=process_node,
        data_nodes=data_nodes,
        node_correlation_ids=node_correlation_ids,
        edges=edges,
    )
