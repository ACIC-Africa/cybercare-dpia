"""Response and request envelopes for the six monitor-configuration routes.

WHY THESE SHAPES ARE NOT NEGOTIABLE. Every model here mirrors an auto-generated
TypeScript type under clients/admin-ui/src/types/api/models/, generated from
Ethyca's own OpenAPI schema. The shipped admin UI reads those fields by name.
Plans 03/03b established what happens otherwise: the response is a valid 200,
the screen is blank, and nothing logs an error. test_response_model_ts_parity.py
holds the line.

Optionality mirrors the TS types rather than the database. Several columns are
NOT NULL with server defaults (databases, excluded_databases, enabled), but the
TS type marks them optional, and a schema stricter than the UI would reject rows
the UI itself considers valid.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fides.api.models.detection_discovery.core import MonitorFrequency


class MonitorStewardUserResponse(BaseModel):
    """Mirrors MonitorStewardUserResponse.ts.

    `username` is required there (no `?`) — unlike `email_address`,
    `first_name`, `last_name`, all of which carry `?` and are genuinely
    optional.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email_address: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class MonitorExecutionResponse(BaseModel):
    """Mirrors MonitorExecution.ts."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    # Required in MonitorExecution.ts (`monitor_config_key: string;`, no `?`)
    # — unlike status/started/completed below, all of which carry `?`. Task 3
    # controller ruling: previously Optional[str] = None, which would have
    # silently accepted a row missing this field.
    monitor_config_key: str
    status: Optional[str] = None
    started: Optional[datetime] = None
    completed: Optional[datetime] = None
    classification_instances: List[str] = Field(default_factory=list)
    messages: List[str] = Field(default_factory=list)


class MonitorStatusResponse(BaseModel):
    """Mirrors MonitorStatusResponse.ts.

    from_attributes reads MonitorConfig's existing properties directly:
    connection_config_key, execution_start_date, execution_frequency and
    classify_params are all Python properties on the model, so there is nothing
    to assemble by hand and nothing to drift.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    key: Optional[str] = None
    connection_config_key: str
    classify_params: Optional[Dict[str, Any]] = None
    datasource_params: Optional[Dict[str, Any]] = None
    databases: List[str] = Field(default_factory=list)
    execution_start_date: Optional[datetime] = None
    execution_frequency: Optional[MonitorFrequency] = None
    excluded_databases: List[str] = Field(default_factory=list)
    enabled: Optional[bool] = None
    shared_config_id: Optional[str] = None
    stewards: List[MonitorStewardUserResponse] = Field(default_factory=list)
    last_monitored: Optional[datetime] = None
    execution_records: Optional[List[MonitorExecutionResponse]] = None


class MonitorConfigResponse(BaseModel):
    """The PUT and GET-one response — mirrors MonitorConfig.ts.

    MonitorConfig.ts and MonitorStatusResponse.ts are distinct generated
    types, not one extending the other: MonitorConfig.ts has exactly 13
    fields and, unlike MonitorStatusResponse.ts, no `execution_records` at
    all — it omits the execution history. Subclassing MonitorStatusResponse
    would silently inherit that field; this is a sibling model instead, with
    its own copy of the 13 shared fields, so the parity gate can check each
    against its own TypeScript counterpart without either dragging in a
    field the other doesn't have.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    key: Optional[str] = None
    connection_config_key: str
    classify_params: Optional[Dict[str, Any]] = None
    datasource_params: Optional[Dict[str, Any]] = None
    databases: List[str] = Field(default_factory=list)
    execution_start_date: Optional[datetime] = None
    execution_frequency: Optional[MonitorFrequency] = None
    excluded_databases: List[str] = Field(default_factory=list)
    enabled: Optional[bool] = None
    shared_config_id: Optional[str] = None
    stewards: List[MonitorStewardUserResponse] = Field(default_factory=list)
    last_monitored: Optional[datetime] = None


class EditableMonitorConfig(BaseModel):
    """The PUT request body — mirrors EditableMonitorConfig.ts.

    `stewards` here is `Array<string>` (steward user IDs to set), NOT
    `Array<MonitorStewardUserResponse>` — EditableMonitorConfig.ts and
    MonitorStatusResponse.ts type the same field name differently: the
    request takes IDs, the response returns hydrated user objects. Without
    both fields, a PUT that sets stewardship or the inherit-from-system flag
    would validate, return 200, and silently drop that data — Pydantic's
    default extra="ignore" would not even complain.
    """

    name: str
    key: Optional[str] = None
    connection_config_key: str
    classify_params: Optional[Dict[str, Any]] = None
    datasource_params: Optional[Dict[str, Any]] = None
    databases: List[str] = Field(default_factory=list)
    excluded_databases: List[str] = Field(default_factory=list)
    execution_start_date: Optional[datetime] = None
    execution_frequency: Optional[MonitorFrequency] = None
    enabled: Optional[bool] = None
    shared_config_id: Optional[str] = None
    stewards: List[str] = Field(default_factory=list)
    inherit_system_stewards: Optional[bool] = None

    @field_validator("key")
    @classmethod
    def key_has_no_dot(cls, value: Optional[str]) -> Optional[str]:
        # monitorconfig carries CheckConstraint ck_monitorconfig_key_no_dots.
        # Rejecting here turns a 500 IntegrityError into a 422 that names the
        # field, which is what the UI can actually show the user.
        if value and "." in value:
            raise ValueError("monitor key may not contain a dot")
        return value


class LinkedDatasetInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fides_key: str
    name: Optional[str] = None


class MonitorDeletionImpact(BaseModel):
    """What deleting this monitor would destroy — mirrors MonitorDeletionImpact.ts.

    Nothing scans until plan 11, so the first three are structurally zero today.
    The route exists now because the UI calls it BEFORE it will allow a delete;
    without it, deletion is unreachable from the screen.
    """

    staged_resource_count: int
    linked_datasets: List[LinkedDatasetInfo] = Field(default_factory=list)
    active_task_count: int = 0
    associated_system_count: int = 0


class DeleteMonitorResponse(BaseModel):
    """The DELETE response — the UI reads `{ count: number }` and nothing else."""

    count: int
