/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * RTK Query slice for the discovery-monitor configuration surface
 * (`src/fides/api/privacycare/api/monitors.py`), mounted at Ethyca's own
 * `/plus/discovery-monitor*` path — see that module's docstring for why
 * PrivacyCare squats the `plus` namespace here (the shipped admin UI's own
 * discovery-monitor screen calls these exact paths) rather than taking its
 * own namespace the way `ropa.slice.ts` does.
 *
 * FOUR OF THE EIGHT ROUTES THAT MODULE EXPOSES ARE USED HERE: list, put
 * (create-or-edit, idempotent by key), execute, and deletion-impact
 * (repurposed as the one live count this screen can show — see
 * DiscoveryScreen.tsx's own comment on why). `delete` and the two
 * `/databases` picker routes exist to serve a create-monitor wizard this
 * screen does not need: the target is fixed to the one already-seeded
 * connection (D-DM-5, `PRIVACYCARE_SCRATCH_CONNECTION_KEY` below), never
 * chosen by a user, so there is nothing to delete and no database to pick
 * — the monitor this screen creates is deliberately unscoped (an empty
 * `databases` list; see `walk.py`'s own docstring) so it walks the whole
 * catalogue rather than one schema of it.
 *
 * WHAT THIS SLICE CANNOT DO, AND WHY THAT IS NOT A BUG HERE. DESIGN.md's
 * Screen 4 also specifies a findings list — one row per discovered table,
 * "Already mapped" / "Ignored" / "Needs review" — and a reconcile action
 * that marks a finding as belonging to a system or ignores it with a
 * reason. No route on this surface, or anywhere else in this build, lists
 * individual `stagedresource` rows or writes a per-resource decision:
 * `discovery/execute.py`'s `reconcile()` only ever diffs a WHOLE scan
 * against the table and is never called from an HTTP route directly, and
 * `PRIVACYCARE_DISCOVERY_READ`'s own scope description ("View discovery
 * monitors and their results") promises a results view this API does not
 * yet serve. `MonitorConfig.last_monitored` is likewise never written by
 * this pipeline (`execute.py`'s `run_monitor` updates only
 * `MonitorExecution`, never the monitor row), so even "last run" is not
 * available through `get_monitor`/`list_monitors`. Full list of what was
 * looked for and not found: docs/design/privacycare-screens/
 * 04-discovery-build-report.md. This slice, and DiscoveryScreen.tsx, build
 * only what these four routes can actually prove.
 */
import { baseApi } from "~/features/common/api.slice";
import {
  EditableMonitorConfig,
  MonitorConfig,
  MonitorDeletionImpact,
  Page_MonitorStatusResponse_,
} from "~/types/api";

// D-DM-5 / scripts/privacycare/seed_connection.py: the ONE ConnectionConfig
// a discovery monitor can bind to today, pointing at our own already-running
// fides-db — never a customer system. Every call this slice makes is scoped
// to monitors bound to THIS connection; nothing here reads, creates, or
// executes a monitor against anything else.
export const PRIVACYCARE_SCRATCH_CONNECTION_KEY =
  "privacycare_scratch_local_postgres";

// A fixed, stable key (never derived from a user-typed name) so `putMonitor`
// is a true create-or-edit against the same row every time this screen's
// "Run scan" control is used — never a second monitor accumulating beside
// the first.
export const PRIVACYCARE_DISCOVERY_MONITOR_KEY = "privacycare_local_discovery";

const discoveryApi = baseApi.injectEndpoints({
  overrideExisting: true,
  endpoints: (build) => ({
    // I2 fix (monitors.py's own docstring): `connection_config_key` is
    // declared so FastAPI does not silently drop it — left off, this would
    // list every monitor in the system, not just ours.
    getDiscoveryMonitors: build.query<Page_MonitorStatusResponse_, void>({
      query: () => ({
        url: "plus/discovery-monitor",
        params: { connection_config_key: PRIVACYCARE_SCRATCH_CONNECTION_KEY },
      }),
      providesTags: ["Discovery Monitor Configs"],
    }),

    // Create-or-edit, PUT being idempotent per key (monitors.py's own
    // docstring). Response model is MonitorConfigResponse, which mirrors
    // MonitorConfig.ts field-for-field (13 fields, no execution_records —
    // see this file's own header comment on why that field is never
    // present anywhere on this surface).
    putDiscoveryMonitor: build.mutation<MonitorConfig, EditableMonitorConfig>({
      query: (body) => ({
        url: "plus/discovery-monitor",
        method: "PUT",
        body,
      }),
      invalidatesTags: ["Discovery Monitor Configs"],
    }),

    // Queues the scan; returns as soon as it is queued (202), never once
    // the walk finishes (execute.py's run_monitor runs off-request, on a
    // Celery worker). A 409 here means a scan is already in progress for
    // this monitor (F3's own guard) — surfaced by the caller via
    // getErrorMessage, not treated as an unexpected failure.
    executeDiscoveryMonitor: build.mutation<{ detail: string }, string>({
      query: (monitorKey) => ({
        url: `plus/discovery-monitor/${monitorKey}/execute`,
        method: "POST",
      }),
      invalidatesTags: ["Discovery Monitor Configs"],
    }),

    // Repurposed for a live count, not a deletion warning: `deletion-impact`
    // runs a real, unfiltered `SELECT count(*) FROM stagedresource WHERE
    // monitor_config_id = :key` (monitors.py's `_STAGED_RESOURCE_COUNT_SQL`)
    // gated on PRIVACYCARE_DISCOVERY_READ, same as everything else on this
    // screen — the only route on this whole surface that reads
    // `stagedresource` at all. It is the one true signal this screen has
    // that a scan found anything; see DiscoveryScreen.tsx for how it is
    // labelled so it is never mistaken for the findings list DESIGN.md asks
    // for and this API cannot yet serve.
    getDiscoveryMonitorDeletionImpact: build.query<
      MonitorDeletionImpact,
      string
    >({
      query: (monitorKey) => ({
        url: `plus/discovery-monitor/${monitorKey}/deletion-impact`,
      }),
      providesTags: (_result, _error, monitorKey) => [
        { type: "Discovery Monitor Configs", id: monitorKey },
      ],
    }),
  }),
});

export const {
  useGetDiscoveryMonitorsQuery,
  usePutDiscoveryMonitorMutation,
  useExecuteDiscoveryMonitorMutation,
  useGetDiscoveryMonitorDeletionImpactQuery,
} = discoveryApi;

export { discoveryApi };
