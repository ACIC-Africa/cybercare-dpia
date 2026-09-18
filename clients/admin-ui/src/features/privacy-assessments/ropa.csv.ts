/**
 * PrivacyCare — Record of processing activities (ROPA), Screen 3 of
 * docs/design/privacycare-screens/DESIGN.md.
 *
 * CSV shape: one row per processing activity, with the process columns
 * repeated on every row — "the shape a regulator expects and a spreadsheet
 * opens" (DESIGN.md). A process with no declarations and no
 * missing_declarations still gets exactly one row, so a spreadsheet reader
 * sees "no processing activity recorded" rather than the process's absence
 * from the file — the same "a quiet hole is worse than an admitted one"
 * principle DESIGN.md states for missing_declarations, applied to the
 * export as well as the screen.
 *
 * ETHYCA CONVENTION MATCHED, AND WHERE IT COULD NOT BE. Ethyca ships a ROPA
 * CSV export for its own `data-purpose` entity
 * (clients/admin-ui/src/features/data-purposes/useDownloadRoPA.ts), but the
 * bytes are generated server-side by ~14 `plus/data-purpose/*` routes that
 * live in Fides Plus — closed-source, not vendored into this OSS repo, and
 * not running in this deployment (plus/health 404s here — see
 * api/processes.py's own module docstring). There is no CSV-generation
 * code in this repository to read, and no running Plus instance to
 * download a real file from, so this module's column *headers* could not
 * be verified byte-for-byte against Ethyca's own export. What WAS matched,
 * from Fides' own OSS CSV export that IS in this repo
 * (service/privacy_request/privacy_request_csv_download.py, the "download
 * privacy requests as CSV" route):
 *   - Title Case, space-separated column headers ("Request Type", not
 *     `request_type`).
 *   - A `download_{date}.csv`-shaped filename
 *     (`privacy_requests_download_{date}.csv` there; `ropa_download_{date}.csv`
 *     here).
 *   - One row per record, static columns first.
 * This is recorded here, not left implicit, because the build prompt asked
 * explicitly which conventions were matched and which could not be.
 *
 * Uses papaparse's `unparse` (already a dependency — `parse` is used
 * elsewhere in this app, e.g. SharedMonitorConfigForm.tsx) rather than
 * hand-rolled comma-joining, so field escaping (embedded commas, quotes,
 * newlines in a free-text description or a data-categories list) is
 * handled by a library already proven in this codebase, not reinvented.
 */
import { unparse } from "papaparse";

import { RopaDeclarationResponse, RopaEntryResponse } from "./ropa.types";

export const ROPA_CSV_FIELDS = [
  "Business Process",
  "Business Cycle",
  "Register Reference",
  "Process Owner",
  "Processing Activity",
  "Purpose Of Processing",
  "Data Categories",
  "Data Subjects",
  "Lawful Basis",
  "Retention Period",
  "System",
  "Notes",
] as const;

const NO_ACTIVITY_NOTE =
  "No processing activity has been recorded for this business process yet.";

const missingDeclarationNote = (declarationId: string) =>
  `This process links to a processing activity that no longer exists (id: ${declarationId}).`;

/** Looks up a taxonomy key's human name, falling back to the key itself —
 * the same fallback useTaxonomies' own getData*DisplayName functions use,
 * so a key this screen has never seen still prints something rather than
 * throwing. Injected rather than imported: useTaxonomies is a React hook
 * and this module has to stay callable from a plain function (the CSV
 * export click handler) and from a Jest test with no taxonomy data loaded
 * at all. */
export interface RopaCsvNameLookup {
  dataUseName: (key: string) => string;
  dataCategoryName: (key: string) => string;
  dataSubjectName: (key: string) => string;
}

const joinNames = (keys: string[], nameFor: (key: string) => string): string =>
  keys.map(nameFor).join("; ");

const processColumns = (
  process: RopaEntryResponse["process"],
): [string, string, string, string] => [
  process.name,
  process.business_cycle ?? "",
  process.external_ref ?? "",
  process.owner_name ?? "",
];

const activityRow = (
  process: RopaEntryResponse["process"],
  declaration: RopaDeclarationResponse,
  names: RopaCsvNameLookup,
): string[] => [
  ...processColumns(process),
  declaration.name ?? "",
  names.dataUseName(declaration.data_use),
  joinNames(declaration.data_categories, names.dataCategoryName),
  joinNames(declaration.data_subjects, names.dataSubjectName),
  declaration.legal_basis ?? "",
  declaration.retention_period ?? "",
  declaration.system_name ?? "",
  "",
];

const noteOnlyRow = (
  process: RopaEntryResponse["process"],
  note: string,
): string[] => [...processColumns(process), "", "", "", "", "", "", "", note];

/** Rows for ONE entry, in the exact shape the header row promises — every
 * caller (the single-entry export and the whole-register export) builds
 * off this so the two can never disagree about column meaning. */
export const ropaEntryToCsvRows = (
  entry: RopaEntryResponse,
  names: RopaCsvNameLookup,
): string[][] => {
  const rows: string[][] = entry.declarations.map((declaration) =>
    activityRow(entry.process, declaration, names),
  );
  entry.missing_declarations.forEach((declarationId) => {
    rows.push(
      noteOnlyRow(entry.process, missingDeclarationNote(declarationId)),
    );
  });
  if (rows.length === 0) {
    rows.push(noteOnlyRow(entry.process, NO_ACTIVITY_NOTE));
  }
  return rows;
};

/** Wraps a set of already-built rows (ROPA_CSV_FIELDS order) into a CSV
 * string. Split out from ropaEntriesToCsv so a single entry's rows
 * (RopaEntry.tsx's own "Export CSV" button, which already has the one
 * entry it needs loaded — no extra fetch) and many entries' rows
 * (RopaList.tsx's "Export register as CSV", which fetches every process
 * first) both produce a file with IDENTICAL column headers, built by the
 * same code. */
export const ropaRowsToCsv = (rows: string[][]): string =>
  unparse({ fields: [...ROPA_CSV_FIELDS], data: rows }, { header: true });

export const ropaEntriesToCsv = (
  entries: RopaEntryResponse[],
  names: RopaCsvNameLookup,
): string =>
  ropaRowsToCsv(entries.flatMap((entry) => ropaEntryToCsvRows(entry, names)));

const todayIsoDate = () => new Date().toISOString().slice(0, 10);

export const ropaCsvFilename = (scope: "process" | "register") =>
  `ropa_${scope}_download_${todayIsoDate()}.csv`;

/** Triggers a browser download of an already-built CSV string — same
 * Blob + anchor + revokeObjectURL sequence
 * `~/features/data-purposes/useDownloadRoPA.ts` uses for Ethyca's own
 * server-generated RoPA CSV, so a download triggered from this screen
 * behaves identically to the one triggered from Data Purposes. */
export const downloadCsv = (csv: string, filename: string): void => {
  const blob = new Blob([csv], { type: "text/csv" });
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
};
