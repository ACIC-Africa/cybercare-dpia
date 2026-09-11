# Projection of a ROPA entry onto Cybercare graph shapes.
#
# PrivacyCare does not write to the Cybercare graph — that is phase 3, and it
# goes through the bucket-1 live-feed contract (ADR-024). What this module
# guarantees is that when the feed is built, it is a projection and not a
# translation: every field has a destination, and the shapes are emitted here
# so a test can assert them without a Cybercare instance in the room.
from dataclasses import dataclass, field

from fides.api.privacycare.ropa import RopaEntry

PROVENANCE = "privacycare"


@dataclass
class GraphProjection:
    process_node: dict
    data_nodes: list[dict] = field(default_factory=list)
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
            edges.append(
                {
                    "organisation_id": organisation_id,
                    "source_id": entry.process.id,
                    "target_id": node_id,
                    "provenance": PROVENANCE,
                }
            )

    return GraphProjection(
        process_node=process_node, data_nodes=data_nodes, edges=edges
    )
