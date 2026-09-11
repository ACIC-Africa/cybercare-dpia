# Phase 3 feeds PrivacyCare's ROPA into the Cybercare graph as a bucket-1
# live feed. The spec promises that projection is mechanical. This test is
# what keeps the promise honest — it runs with no Cybercare present.
import pytest

from fides.api.privacycare.graph_projection import project_to_graph
from fides.api.privacycare.ropa import RopaDeclaration, RopaEntry

PROCESS_NODE_KEYS = {"organisation_id", "name", "description", "provenance"}
DATA_NODE_KEYS = {
    "organisation_id",
    "name",
    "pii_category",
    "lawful_basis",
    "retention_policy",
    "provenance",
}
EDGE_KEYS = {"organisation_id", "source_id", "target_id", "provenance"}


class _Proc:
    id = "proc-1"
    name = "Fuel Dealer Onboarding"
    description = "Vetting and contracting a new retail dealer"


def _entry():
    return RopaEntry(
        process=_Proc(),
        declarations=[
            RopaDeclaration(
                id="decl-1",
                name="Dealer vetting",
                data_use="essential.service",
                data_categories=["user.contact", "user.government_id"],
                data_subjects=["customer"],
                legal_basis="Contract",
                retention_period="7y",
                system_id="sys-1",
                system_name="Dealer Portal",
            )
        ],
    )


def test_process_node_shape_matches_the_graph():
    proj = project_to_graph(_entry(), organisation_id="org-1")
    assert set(proj.process_node) == PROCESS_NODE_KEYS
    assert proj.process_node["name"] == "Fuel Dealer Onboarding"
    assert proj.process_node["organisation_id"] == "org-1"
    assert proj.process_node["provenance"] == "privacycare"


def test_each_data_category_becomes_one_data_node():
    proj = project_to_graph(_entry(), organisation_id="org-1")
    assert len(proj.data_nodes) == 2, "one node per data category"
    for node in proj.data_nodes:
        assert set(node) == DATA_NODE_KEYS
        assert node["lawful_basis"] == "Contract"
        assert node["retention_policy"] == "7y"


def test_one_edge_per_data_node():
    proj = project_to_graph(_entry(), organisation_id="org-1")
    assert len(proj.edges) == len(proj.data_nodes)
    for edge in proj.edges:
        assert set(edge) == EDGE_KEYS
        assert edge["source_id"] == "proc-1"


def test_an_empty_ropa_projects_to_a_process_with_no_data():
    entry = RopaEntry(process=_Proc())
    proj = project_to_graph(entry, organisation_id="org-1")
    assert proj.data_nodes == []
    assert proj.edges == []
    assert proj.process_node["name"] == "Fuel Dealer Onboarding"
