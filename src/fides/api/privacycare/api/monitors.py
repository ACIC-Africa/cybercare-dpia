"""The six routes that make a discovery monitor configurable.

NAMESPACE. These routes squat Ethyca's `plus` namespace
(`/plus/discovery-monitor*`), NOT our own `privacycare/` namespace the way
api/processes.py's business-process ROPA surface does. The reason is the
same one api/router.py's PRIVACYCARE_PREFIX comment gives for the assessment
routes: the shipped admin UI's discovery-monitor screen
(clients/admin-ui/src/features/data-discovery-and-detection/
discovery-detection.slice.ts — getMonitorsByIntegration, putDiscoveryMonitor,
getDatabasesByMonitor, deleteDiscoveryMonitor, getMonitorDeletionImpact, and
action-center.slice.ts's getMonitorConfig) calls these exact paths, and the
UI's path is the requirement (spec D-DM-6). Where the UI does not constrain
us — the business-process ROPA surface, which the UI has no screen for at
all — we take our own namespace instead; where it does, as here, we match it.

SCOPE. Only the six routes the UI's discovery-monitor screen actually calls
to configure a monitor: list, create/edit (PUT is idempotent per key, so it
serves both), read one, delete, the pre-delete impact check, and the
database/schema picker. Nothing here executes a monitor, classifies
anything, or writes a StagedResource row — that is plan 11's job.
`get_monitor_databases` reads schema NAMES through `inspect()`; it reads no
row of data and is not a scan.

`{monitor_config_id}` IN THE UI'S URLS IS THE MONITOR'S `key`, not its row
id — action-center.slice.ts's `getMonitorConfig` passes exactly what
`MonitorStatusResponse.key` returned. Every lookup below is by `key`.
"""
import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.models.connectionconfig import ConnectionConfig
from fides.api.models.detection_discovery.core import MonitorConfig
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.monitor_schemas import (
    DeleteMonitorResponse,
    EditableMonitorConfig,
    MonitorConfigResponse,
    MonitorDeletionImpact,
    MonitorStatusResponse,
)
from fides.api.privacycare.api.router import privacycare_monitors_router
from fides.api.service.connectors import get_connector
from fides.common.scope_registry import (
    PRIVACYCARE_DISCOVERY_READ,
    PRIVACYCARE_DISCOVERY_UPDATE,
)

# stagedresource.monitor_config_id is a "soft pointer" column (see its own
# comment in models/detection_discovery/core.py) that in practice holds the
# monitor's KEY, not its row id — confirmed by alembic migration
# 2f3c1a2d6b10 ("disallow_dot_in_monitor_key_and_update_refs"), which
# rewrites stagedresource.monitor_config_id in lockstep with
# monitorconfig.key whenever a key changes. monitortask.monitor_config_id,
# by contrast, is a real foreign key to monitorconfig.id (see
# models/detection_discovery/monitor_task.py) — the two tables disagree on
# which of the monitor's two identifiers they point at, so the two queries
# below bind different values on purpose.
_STAGED_RESOURCE_COUNT_SQL = sqlalchemy.text(
    "SELECT count(*) FROM stagedresource WHERE monitor_config_id = :key"
)

# ExecutionLogStatus's terminal values (models/worker_task.py): a task that
# has reached one of these will not become active again. Everything else —
# in_processing, pending, the "paused" awaiting_processing state, retrying,
# polling — still counts as active for a pre-delete warning, since a delete
# would cancel it while it could still do something.
_ACTIVE_MONITOR_TASK_COUNT_SQL = sqlalchemy.text(
    "SELECT count(*) FROM monitortask WHERE monitor_config_id = :id "
    "AND status NOT IN ('complete', 'error', 'skipped')"
)


def _monitor_or_404(db: Session, key: str) -> MonitorConfig:
    monitor = db.query(MonitorConfig).filter(MonitorConfig.key == key).first()
    if monitor is None:
        raise HTTPException(
            status_code=status_codes.HTTP_404_NOT_FOUND,
            detail=f"No monitor with key {key}",
        )
    return monitor


@privacycare_monitors_router.get(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=Page[MonitorStatusResponse],
)
def list_monitors(
    params: Params = Depends(),
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ]),
) -> Page[MonitorStatusResponse]:
    """Every configured monitor, for the discovery-monitor list screen."""
    monitors = (
        db.query(MonitorConfig).order_by(MonitorConfig.name, MonitorConfig.key).all()
    )
    return paginate(
        [MonitorStatusResponse.model_validate(m) for m in monitors], params
    )


@privacycare_monitors_router.put(
    "",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE])],
    response_model=MonitorConfigResponse,
)
def put_monitor(
    request: EditableMonitorConfig,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE]),
) -> MonitorConfigResponse:
    """Create or edit a monitor. The UI's save button PUTs the whole config
    every time, so a PUT naming an existing key is an edit, never a second
    monitor."""
    connection = (
        db.query(ConnectionConfig)
        .filter(ConnectionConfig.key == request.connection_config_key)
        .first()
    )
    if connection is None:
        # connection_config_id is NOT NULL with a foreign key; without this the
        # failure is an IntegrityError 500 rather than a message naming the key.
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST,
            detail=f"No connection configuration with key {request.connection_config_key}",
        )

    data = request.model_dump(exclude={"connection_config_key"})
    data["connection_config_id"] = connection.id

    # `enabled` and `inherit_system_stewards` are NOT NULL columns on
    # monitorconfig (server_default "t") but Optional[bool] = None on
    # EditableMonitorConfig, mirroring EditableMonitorConfig.ts where both
    # carry `?`. MonitorConfig.create()'s INSERT happens to fall back to the
    # column's Python-side default when the value is None, but .update() has
    # no equivalent fallback for UPDATE — SQLAlchemy's Column `default=` only
    # fires on INSERT — so leaving either field out of a PUT would otherwise
    # send an explicit NULL on every edit and raise a NotNullViolation
    # instead of leaving the existing value alone. Dropped rather than sent,
    # so a caller who doesn't mention them can't touch them.
    for optional_flag in ("enabled", "inherit_system_stewards"):
        if data.get(optional_flag) is None:
            data.pop(optional_flag, None)

    existing = db.query(MonitorConfig).filter(MonitorConfig.key == request.key).first()
    # create() and update() are overridden on MonitorConfig and carry the
    # databases / excluded_databases validation. Never write the row directly.
    monitor = (
        existing.update(db=db, data=data)
        if existing
        else MonitorConfig.create(db=db, data=data)
    )
    db.commit()
    return MonitorConfigResponse.model_validate(monitor)


@privacycare_monitors_router.get(
    "/{monitor_config_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=MonitorConfigResponse,
)
def get_monitor(
    monitor_config_id: str,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ]),
) -> MonitorConfigResponse:
    """One monitor's full configuration, by key."""
    monitor = _monitor_or_404(db, monitor_config_id)
    return MonitorConfigResponse.model_validate(monitor)


@privacycare_monitors_router.delete(
    "/{monitor_config_id}",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE])],
    response_model=DeleteMonitorResponse,
)
def delete_monitor(
    monitor_config_id: str,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE]),
) -> DeleteMonitorResponse:
    """Delete a monitor. The UI reads only `{ count }` from the response —
    deleteDiscoveryMonitor types its RTK Query mutation as
    `build.mutation<{ count: number }, ...>`, not against a named type."""
    monitor = _monitor_or_404(db, monitor_config_id)
    db.delete(monitor)
    db.commit()
    return DeleteMonitorResponse(count=1)


@privacycare_monitors_router.get(
    "/{monitor_config_id}/deletion-impact",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=MonitorDeletionImpact,
)
def get_monitor_deletion_impact(
    monitor_config_id: str,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ]),
) -> MonitorDeletionImpact:
    """What deleting this monitor would destroy. The UI calls this BEFORE it
    will allow a delete, so a missing route blocks deletion entirely — hence
    this route exists now even though nothing scans until plan 11.

    `staged_resource_count` and `active_task_count` run real queries against
    `stagedresource` and `monitortask`, even though both tables are empty
    today: plan 11 fills them and this route must already be correct when it
    does. `linked_datasets` and `associated_system_count` stay at their
    schema defaults (empty / zero) — nothing in this schema links a monitor
    to a Fides Dataset or System yet, so there is no row for either to count;
    that linkage is also plan 11's.
    """
    monitor = _monitor_or_404(db, monitor_config_id)
    staged_resource_count = db.execute(
        _STAGED_RESOURCE_COUNT_SQL, {"key": monitor.key}
    ).scalar()
    active_task_count = db.execute(
        _ACTIVE_MONITOR_TASK_COUNT_SQL, {"id": monitor.id}
    ).scalar()
    return MonitorDeletionImpact(
        staged_resource_count=staged_resource_count,
        active_task_count=active_task_count,
    )


@privacycare_monitors_router.get(
    "/{monitor_config_id}/databases",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=Page[str],
)
def get_monitor_databases(
    monitor_config_id: str,
    params: Params = Depends(),
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ]),
) -> Page[str]:
    """What this connection exposes, so a consultant can scope the monitor.

    D-DM-4: a live read against the target through the same `inspect()` call
    sql_connector.py already makes. It lists scope units by name and reads no
    row of data — it is not a scan, and it writes nothing.
    """
    monitor = _monitor_or_404(db, monitor_config_id)
    connector = get_connector(monitor.connection_config)
    try:
        engine = connector.create_client()
        with engine.connect() as connection:
            names = sqlalchemy.inspect(connection).get_schema_names()
    except Exception as error:  # noqa: BLE001 — the target's failure, not ours
        # An unreachable or misconfigured target is the target's problem. 502
        # says so; a 500 would read as a bug in PrivacyCare.
        raise HTTPException(
            status_code=status_codes.HTTP_502_BAD_GATEWAY,
            detail=(
                "Could not read databases from connection "
                f"{monitor.connection_config.key}: {error}"
            ),
        ) from error
    return paginate(sorted(names), params)
