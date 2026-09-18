/**
 * PrivacyCare — Discovery, Screen 4 of docs/design/privacycare-screens/DESIGN.md.
 *
 * WHY THIS FILE EXISTS — A REAL GAP IN THE "DONE" API, NAMED RATHER THAN
 * WORKED AROUND SILENTLY.
 *
 * Reconciling a finding as "mapped" requires a `system_id`
 * (ReconcileFindingRequest.system_id) — and discovery.findings._system_exists
 * validates it against `ctl_systems.id`, the internal database id (verified
 * live: `SELECT 1 FROM ctl_systems WHERE id = :id`), NOT `fides_key`.
 * Confirmed against the live database that the two differ for every real
 * system (e.g. `id='sys_4481f3505f6b'`, `fides_key='privacycare_process_bp_9e332dcbe707'`).
 *
 * But NO read route anywhere in this codebase — not Ethyca's `GET /system`
 * family, not `SystemSelect`, not PrivacyCare's own ROPA or screening
 * surfaces — ever puts that internal `id` on the wire for general system
 * selection. Confirmed against the live OpenAPI schema: `SystemResponse`
 * and `BasicSystemResponseExtended` declare no `id` property at all; every
 * other place a system is identified in this codebase (`SystemSelect`,
 * `user_assigned_system_key` in Ethyca's own action-center) uses
 * `fides_key`. Sending a `fides_key` as this route's `system_id` would 400
 * with "unknown system" for any system whose `id` happens not to equal its
 * `fides_key` — true of every system checked. There is no Python edit in
 * scope for this task (the API is done) — this is reported, not patched.
 *
 * THE WORKAROUND, SCOPED HONESTLY. The only place `ctl_systems.id` legally
 * reaches this screen at all is inside a discovery FINDING already reconciled
 * as "mapped" — `FindingResponse.system_id` / `system_name`, resolved
 * server-side from a real `privacycare_discovery_reconciliation` row. So the
 * "mark as mapped" picker below offers exactly the systems some OTHER
 * finding has already been mapped to — a real, always-correct id, at the
 * cost of being unable to name a system discovery has never mapped to
 * before. That is DESIGN.md's own words for this state ("already in the
 * data map") read narrowly but truthfully, and it is the same "state the
 * gap in the privacy officer's own words" discipline this project already
 * applies elsewhere (see 04-discovery-build-report.md, the earlier build's
 * own admission).
 */
import { useMemo } from "react";

import { FindingResponse } from "./discovery-findings.types";

export interface SystemCandidate {
  systemId: string;
  systemName: string;
}

/**
 * Every system currently reachable as a "mark as mapped" target, derived
 * from the findings already loaded on this screen (never a second network
 * call) — deduplicated by `system_id`, sorted by name.
 */
export const useSystemCandidatesFromFindings = (
  findings: FindingResponse[] | undefined,
): SystemCandidate[] =>
  useMemo(() => {
    const byId = new Map<string, string>();
    (findings ?? []).forEach((finding) => {
      if (finding.state === "mapped" && finding.system_id) {
        byId.set(finding.system_id, finding.system_name ?? finding.system_id);
      }
    });
    return [...byId.entries()]
      .map(([systemId, systemName]) => ({ systemId, systemName }))
      .sort((a, b) => a.systemName.localeCompare(b.systemName));
  }, [findings]);
