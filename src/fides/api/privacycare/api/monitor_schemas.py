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


class MonitorClassifyParamsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="allow")


class MonitorStewardUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: Optional[str] = None
    email_address: Optional[str] = None


class MonitorExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    monitor_config_key: Optional[str] = None
    status: Optional[str] = None
    started: Optional[datetime] = None
    completed: Optional[datetime] = None


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


class MonitorConfigResponse(MonitorStatusResponse):
    """The PUT and GET-one response.

    MonitorConfig.ts and MonitorStatusResponse.ts carry the same fields; they
    are distinct types in the UI, so they are distinct here, and the parity gate
    checks each against its own TypeScript counterpart.
    """


class EditableMonitorConfig(BaseModel):
    """The PUT request body — mirrors EditableMonitorConfig.ts."""

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
