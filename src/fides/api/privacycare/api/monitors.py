"""The seven routes that make a discovery monitor configurable.

NAMESPACE. These routes squat Ethyca's `plus` namespace
(`/plus/discovery-monitor*`), NOT our own `privacycare/` namespace the way
api/processes.py's business-process ROPA surface does. The reason is the
same one api/router.py's PRIVACYCARE_PREFIX comment gives for the assessment
routes: the shipped admin UI's discovery-monitor screen
(clients/admin-ui/src/features/data-discovery-and-detection/
discovery-detection.slice.ts — getMonitorsByIntegration, putDiscoveryMonitor,
getDatabasesByMonitor, getAvailableDatabasesByConnection,
deleteDiscoveryMonitor, getMonitorDeletionImpact, and
action-center.slice.ts's getMonitorConfig) calls these exact paths, and the
UI's path is the requirement (spec D-DM-6). Where the UI does not constrain
us — the business-process ROPA surface, which the UI has no screen for at
all — we take our own namespace instead; where it does, as here, we match it.

SCOPE. Only the seven routes the UI's discovery-monitor screen actually
calls to configure a monitor: list, create/edit (PUT is idempotent per key,
so it serves both), read one, delete, the pre-delete impact check, and TWO
database/schema pickers. Nothing here executes a monitor, classifies
anything, or writes a StagedResource row — that is plan 11's job. Both
`get_monitor_databases` and `get_available_databases` read schema NAMES
through `inspect()`; neither reads a row of data, and neither is a scan.

TWO DATABASES ENDPOINTS, NOT ONE (Task 4 fix round 2). The original spec's
enumeration regex ran `discovery-detection.slice.ts`'s two distinct
`databases` query definitions together and only one survived into the
brief: `getDatabasesByMonitor` -> `GET /{monitor_config_id}/databases`
(`get_monitor_databases` below). Final-review finding M11: that hook
(`useGetDatabasesByMonitorQuery`) has zero consumers anywhere under
clients/admin-ui — the shipped wizard's picker calls only
`getAvailableDatabasesByConnection` below. The route is kept anyway: it is
a correct, already-tested read (existing monitor -> its connection's
schemas) and plan 12's results surface is the shape of thing that would
want "what does THIS monitor see" rather than "what does this connection
see before any monitor exists" — so this stays as the route plan 12 is
expected to wire a consumer to, not dead code to delete.

The UI's create-monitor wizard, by contrast, must list a connection's
databases BEFORE any monitor exists to save the picker's selections into —
it calls `getAvailableDatabasesByConnection` -> `POST /databases` with a
monitor-shaped body (`{name, connection_config_key, classify_params}`),
which `get_available_databases` below serves. The two routes share the same
underlying listing logic (`_list_databases`) and differ only in how they
resolve which `ConnectionConfig` to read: by way of an existing monitor's
`connection_config`, or directly by the key the request body names.

`{monitor_config_id}` IN THE UI'S URLS IS THE MONITOR'S `key`, not its row
id — action-center.slice.ts's `getMonitorConfig` passes exactly what
`MonitorStatusResponse.key` returned. Every lookup below is by `key`.
"""
from typing import List, Optional, cast

import sqlalchemy
from fastapi import Depends, HTTPException, Security
from fastapi import status as status_codes
from fastapi_pagination import Page, Params, paginate
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fides.api.common_exceptions import KeyOrNameAlreadyExists, KeyValidationError
from fides.api.deps import get_db
from fides.api.models.client import ClientDetail
from fides.api.models.connectionconfig import ConnectionConfig
from fides.api.models.detection_discovery.core import MonitorConfig
from fides.api.models.fides_user import FidesUser
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.monitor_schemas import (
    DeleteMonitorResponse,
    EditableMonitorConfig,
    MonitorConfigResponse,
    MonitorDeletionImpact,
    MonitorStatusResponse,
)
from fides.api.privacycare.api.router import privacycare_monitors_router
from fides.api.privacycare.discovery.execute import (
    STATUS_IN_PROGRESS,
    execute_monitor_task,
)
from fides.api.service.connectors import get_connector
from fides.api.tasks import DISCOVERY_MONITORS_DETECTION_QUEUE_NAME
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

# F3 (second half): nothing before this fix stopped a double-clicked Scan
# button, or a retry racing a still-running execution, from queuing two
# `run_monitor` calls against the same monitor. Both would walk the same
# catalogue from the same pre-state and reconcile it independently —
# harmless to `stagedresource` itself now that reconcile()'s own INSERT is
# `ON CONFLICT DO NOTHING` (see reconcile.py), but still two redundant scans
# hitting the target concurrently, and two `MonitorExecution` rows racing to
# be "the" record of one logical run. Checked by key, matching every other
# lookup on this surface (`monitorexecution.monitor_config_key` holds the
# monitor's key, same as `stagedresource.monitor_config_id` — see this
# module's own note on that column above `_STAGED_RESOURCE_COUNT_SQL`).
# Not perfectly race-free on its own (a check-then-queue has the same
# TOCTOU gap any such check does), but it closes the common case this
# finding names: a user clicking Scan twice while the first run is still
# `In progress`.
_ACTIVE_EXECUTION_COUNT_SQL = sqlalchemy.text(
    "SELECT count(*) FROM monitorexecution WHERE monitor_config_key = :key "
    "AND status = :in_progress"
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
    connection_config_key: Optional[str] = None,
    params: Params = Depends(),
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ]),
) -> Page[MonitorStatusResponse]:
    """Every configured monitor, for the discovery-monitor list screen.

    I2 fix: the Integrations monitor tab calls this with
    `connection_config_key` set to the integration it's showing
    (useMonitorConfigTable.tsx -> useGetMonitorsByIntegrationQuery), and the
    RTK Query slice spreads the whole params object through untouched
    (discovery-detection.slice.ts). Left undeclared, FastAPI silently drops
    the param and every integration's tab lists every monitor in the
    system — invisible with one connection, wrong the moment a second
    exists. When the param is present, filter to monitors whose connection
    matches it by joining on the connection's key (MonitorConfig only
    stores connection_config_id).
    """
    query = db.query(MonitorConfig)
    if connection_config_key is not None:
        query = query.join(
            ConnectionConfig, MonitorConfig.connection_config_id == ConnectionConfig.id
        ).filter(ConnectionConfig.key == connection_config_key)
    monitors = query.order_by(MonitorConfig.name, MonitorConfig.key).all()
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

    # C1 fix: MonitorConfig.stewards is a many-to-many relationship to
    # FidesUser (secondary="monitorsteward"), not a plain column — passing
    # the request's `stewards: List[str]` (user IDs) straight into
    # data/create()/update() assigns raw strings to a relationship
    # collection and blows up with an AttributeError deep inside SQLAlchemy
    # (500). Popped out of `data` here, resolved to real FidesUser rows
    # below, and assigned to the relationship separately once the monitor
    # row itself exists. This is the DEFAULT path, not an edge case: the
    # steward picker is the third field on the create modal and is re-sent
    # on every enable/disable toggle (MonitorConfigEnableCell.tsx).
    data = request.model_dump(exclude={"connection_config_key", "stewards"})
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

    # Resolve steward IDs to real FidesUser rows BEFORE writing the monitor,
    # so an unknown ID is rejected with a 400 naming it rather than silently
    # dropped or blown up mid-write.
    stewards: List[FidesUser] = []
    if request.stewards:
        stewards = (
            db.query(FidesUser).filter(FidesUser.id.in_(request.stewards)).all()
        )
        found_ids = {user.id for user in stewards}
        missing_ids = [sid for sid in request.stewards if sid not in found_ids]
        if missing_ids:
            raise HTTPException(
                status_code=status_codes.HTTP_400_BAD_REQUEST,
                detail=f"No user(s) with id: {', '.join(missing_ids)}",
            )

    existing = db.query(MonitorConfig).filter(MonitorConfig.key == request.key).first()
    # create() and update() are overridden on MonitorConfig and carry the
    # databases / excluded_databases validation. Never write the row directly.
    #
    # I3 fix: base_class.create() derives a snake-cased key from `name` when
    # none is given and raises KeyOrNameAlreadyExists on either a key OR a
    # name collision (check_name=True, the default here) — the shipped
    # create form sends no key, so two monitors named the same thing on two
    # integrations is a live collision path, not a theoretical one.
    # MonitorConfig.database_include_exclude_list_is_valid raises a bare
    # ValueError when both databases and excluded_databases are set.
    # KeyValidationError is the third member of that family. All three are
    # plain Exception subclasses (KeyOrNameAlreadyExists/KeyValidationError)
    # or a generic ValueError, so none of them are HTTPExceptions on their
    # own — left uncaught they escape as an unhandled 500. Caught here and
    # turned into a 400 naming the reason. IntegrityError (M14: two
    # concurrent PUTs of the same new key both find `existing is None` and
    # both call create()) is a real, lower-level DB conflict and gets its
    # own 409 instead.
    try:
        # cast: MonitorConfig.update()'s declared return type is the base
        # class's FidesBase (it doesn't narrow the override's signature),
        # but both branches actually return this same MonitorConfig row.
        monitor = cast(
            MonitorConfig,
            existing.update(db=db, data=data)
            if existing
            else MonitorConfig.create(db=db, data=data),
        )
        # MonitorConfig.stewards is declared with classic
        # `relationship(FidesUser, secondary=...)`, no `Mapped[...]`
        # annotation (that model is Ethyca-authored and out of scope for
        # this wave), so the sqlalchemy mypy plugin infers a scalar
        # FidesUser rather than the list a secondary-table relationship
        # actually holds — hence the ignore below.
        monitor.stewards = stewards  # type: ignore[assignment]
        db.commit()
    except (KeyOrNameAlreadyExists, KeyValidationError, ValueError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status_codes.HTTP_409_CONFLICT,
            detail=f"Monitor {request.key or request.name} conflicts with a "
            "concurrent write; reload and try again.",
        ) from exc
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
    `build.mutation<{ count: number }, ...>`, not against a named type.

    Fix round 1, Finding 3: the UI also sends a `delete_staged_resources`
    query param (discovery-detection.slice.ts's deleteDiscoveryMonitor,
    default `true`), which this route deliberately neither declares nor
    reads. Harmless today — no StagedResource row can exist yet (that's
    plan 11) — but the moment plan 11's scanning lands, deleting a monitor
    with live StagedResource rows will need to honour this flag (cascade the
    delete vs. orphan them), and get_monitor_deletion_impact's
    `staged_resource_count` will need to stay the number this flag is
    warning the user about.
    """
    monitor = _monitor_or_404(db, monitor_config_id)
    db.delete(monitor)
    db.commit()
    return DeleteMonitorResponse(count=1)


@privacycare_monitors_router.post(
    "/{monitor_config_id}/execute",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE])],
    status_code=status_codes.HTTP_202_ACCEPTED,
)
def execute_monitor(
    monitor_config_id: str,
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_UPDATE]),
) -> dict:
    """Queue a discovery scan. This is the route plan 10 deliberately left
    out: running a scan is a privileged act, not a read, hence
    PRIVACYCARE_DISCOVERY_UPDATE rather than the _READ scope every other
    route on this surface but `put_monitor`/`delete_monitor` carries.

    Returns as soon as the job is queued rather than waiting for the scan to
    finish -- walking a real catalogue can take a while, and the shipped
    UI's mutation (`executeDiscoveryMonitor`, discovery-detection.slice.ts)
    types its response as `any` and reads nothing from it; it only
    invalidates the "Discovery Monitor Configs" tag to re-poll status. Work
    itself -- the walk, the reconcile, and recording the
    `MonitorExecution` row -- all happens in `execute_monitor_task` /
    `run_monitor` (discovery/execute.py), off the request.

    F3: raises 409 rather than queuing a second scan while one is already
    `In progress` for this monitor -- a double-clicked Scan button, or a
    retry racing a still-running execution, would otherwise walk the same
    catalogue twice concurrently and leave two `MonitorExecution` rows
    racing to record one logical run.
    """
    monitor = _monitor_or_404(db, monitor_config_id)

    # F3: refuse a second scan while one is already `In progress` for this
    # monitor -- see `_ACTIVE_EXECUTION_COUNT_SQL`'s own comment.
    active_executions = db.execute(
        _ACTIVE_EXECUTION_COUNT_SQL,
        {"key": monitor.key, "in_progress": STATUS_IN_PROGRESS},
    ).scalar()
    if active_executions:
        raise HTTPException(
            status_code=status_codes.HTTP_409_CONFLICT,
            detail=f"A discovery scan is already in progress for monitor {monitor.key}",
        )

    execute_monitor_task.apply_async(
        args=[monitor.key], queue=DISCOVERY_MONITORS_DETECTION_QUEUE_NAME
    )
    logger.info("PrivacyCare discovery scan queued for monitor {}", monitor.key)
    return {"detail": f"Discovery scan queued for monitor {monitor.key}"}


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


def _list_databases(connection: ConnectionConfig) -> list[str]:
    """What `connection` exposes, so a consultant can scope a monitor.

    D-DM-4: a live read against the target through the same `inspect()` call
    sql_connector.py already makes. It lists scope units by name and reads no
    row of data — it is not a scan, and it writes nothing. Shared by
    `get_monitor_databases` (an existing monitor's connection) and
    `get_available_databases` (a connection named directly, before any
    monitor exists) — factored out so the connector/inspect/except block
    exists exactly once rather than twice with the same 502 contract.
    """
    # I7 fix: get_connector() moved INSIDE the try. The docstring above
    # promises both routes share "the same 502 contract" for the target's
    # failures, but get_connector() raises NotImplementedError for an
    # unsupported connection_type and AttributeError on a null
    # connection_type — sitting above the try, those escaped as a 500
    # attributed to us instead of the 502-attributed-to-the-target contract
    # this function exists to draw.
    engine = None
    try:
        connector = get_connector(connection)
        engine = connector.create_client()
        with engine.connect() as sql_connection:
            names = sqlalchemy.inspect(sql_connection).get_schema_names()
    except Exception as error:  # noqa: BLE001 — the target's failure, not ours
        # I6 fix: the detail used to interpolate {error} — the raw target
        # exception — into a 502 that PRIVACYCARE_DISCOVERY_READ (Viewer and
        # Data Steward, not just an admin) can see. create_client() sets
        # hide_parameters = not CONFIG.dev_mode, so in dev mode SQLAlchemy's
        # error strings carry bound parameters, and an ArgumentError on a
        # malformed URI renders the URI itself — built from `secrets`,
        # containing the password. The connection KEY is enough for a
        # consultant to act on; the underlying exception is logged
        # server-side only, never returned to the caller.
        logger.warning(
            "Could not read databases from connection {}: {}", connection.key, error
        )
        raise HTTPException(
            status_code=status_codes.HTTP_502_BAD_GATEWAY,
            detail=f"Could not read databases from connection {connection.key}",
        ) from error
    finally:
        # I4 fix: create_client() builds a NEW Engine every call (not the
        # cached `client` property) and this function never disposed of it —
        # `with engine.connect()` only returns the pooled connection to that
        # engine's pool, which then stays open until GC. The wizard calls
        # the POST route on mount and per page, so opening it repeatedly
        # accumulated open server-side connections against whatever the
        # connection points at.
        if engine is not None:
            engine.dispose()
    # M12: information_schema is Postgres/MySQL system catalog metadata, not
    # a real scope unit — nothing should ever be scanned there. Filtered out
    # here so neither databases route offers it and the picker never renders
    # it as a choice.
    return sorted(name for name in names if name != "information_schema")


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
    """What an EXISTING monitor's connection exposes. See `_list_databases`."""
    monitor = _monitor_or_404(db, monitor_config_id)
    return paginate(_list_databases(monitor.connection_config), params)


@privacycare_monitors_router.post(
    "/databases",
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ])],
    response_model=Page[str],
)
def get_available_databases(
    request: EditableMonitorConfig,
    params: Params = Depends(),
    db: Session = Depends(get_db),
    client: ClientDetail = Security(verify_oauth_client, scopes=[PRIVACYCARE_DISCOVERY_READ]),
) -> Page[str]:
    """What a connection exposes, named directly — for the create-monitor
    wizard's database picker, which must list a connection's databases
    BEFORE any monitor row exists to attach them to (Task 4 fix round 2,
    Finding 1).

    The request body is EditableMonitorConfig because that is the exact
    shape `getAvailableDatabasesByConnection` sends
    (discovery-detection.slice.ts: `{name: "new-monitor",
    connection_config_key, classify_params: {}}`) — it LOOKS like a create
    request, and it is NOT one: only `connection_config_key` is read.
    `name` and `classify_params` arrive and are ignored. This route is
    `GET`-scoped (PRIVACYCARE_DISCOVERY_READ, not _UPDATE) and writes
    nothing — no `MonitorConfig` row is created, read, or touched by this
    call, POST verb notwithstanding; the verb is POST only because the UI
    sends a body (a GET with a JSON body is non-standard) and FastAPI
    doesn't route based on intent.
    """
    connection = (
        db.query(ConnectionConfig)
        .filter(ConnectionConfig.key == request.connection_config_key)
        .first()
    )
    if connection is None:
        # Same 400 contract as put_monitor: a missing connection names the
        # key rather than surfacing as an unrelated 500.
        raise HTTPException(
            status_code=status_codes.HTTP_400_BAD_REQUEST,
            detail=f"No connection configuration with key {request.connection_config_key}",
        )
    return paginate(_list_databases(connection), params)
