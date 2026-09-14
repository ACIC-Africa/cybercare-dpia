"""Envelopes for the six monitor-configuration routes.

The admin UI's auto-generated TypeScript types are the specification. Plans
03/03b established the failure mode: a technically-correct response in the
wrong envelope renders an empty screen and reports no error anywhere.
"""
import pytest
from pydantic import ValidationError

from fides.api.privacycare.api.monitor_schemas import (
    EditableMonitorConfig,
    MonitorDeletionImpact,
    MonitorStatusResponse,
)


def test_status_response_carries_every_field_the_ui_reads():
    # Enumerated from clients/admin-ui/src/types/api/models/MonitorStatusResponse.ts.
    expected = {
        "name", "key", "connection_config_key", "classify_params",
        "datasource_params", "databases", "execution_start_date",
        "execution_frequency", "excluded_databases", "enabled",
        "shared_config_id", "stewards", "last_monitored", "execution_records",
    }
    assert set(MonitorStatusResponse.model_fields) == expected


def test_only_name_and_connection_key_are_required():
    # Every other field is optional in the TS type; a stricter schema would
    # reject rows the UI itself considers valid.
    schema = MonitorStatusResponse(name="n", connection_config_key="c")
    assert schema.key is None
    assert schema.databases == []
    assert schema.excluded_databases == []


def test_status_response_serialises_straight_off_a_model_row():
    # from_attributes against the real MonitorConfig properties, not a dict
    # assembled by hand — if a property is renamed upstream this fails loudly.
    class FakeRow:
        name = "Retail Postgres"
        key = "retail_pg"
        connection_config_key = "privacycare_scratch_local_postgres"
        classify_params = {}
        datasource_params = None
        databases = ["fides"]
        execution_start_date = None
        execution_frequency = None
        excluded_databases = []
        enabled = True
        shared_config_id = None
        stewards = []
        last_monitored = None
        execution_records = None

    schema = MonitorStatusResponse.model_validate(FakeRow())
    assert schema.name == "Retail Postgres"
    assert schema.connection_config_key == "privacycare_scratch_local_postgres"


def test_editable_config_rejects_a_key_containing_a_dot():
    # The model carries CheckConstraint ck_monitorconfig_key_no_dots. Rejecting
    # it at the schema means a clear 422 instead of an IntegrityError 500.
    with pytest.raises(ValidationError, match="dot"):
        EditableMonitorConfig(name="n", key="has.dot", connection_config_key="c")


def test_editable_config_accepts_a_key_without_one():
    assert EditableMonitorConfig(
        name="n", key="no_dot", connection_config_key="c"
    ).key == "no_dot"


def test_deletion_impact_defaults_to_zero_not_null():
    # Nothing scans yet, so every count is structurally zero. The UI reads these
    # before it will allow a delete; null would render as blank, not as "none".
    impact = MonitorDeletionImpact(staged_resource_count=0)
    assert impact.linked_datasets == []
    assert impact.active_task_count == 0
    assert impact.associated_system_count == 0
