import {
  ROPA_CSV_FIELDS,
  ropaCsvFilename,
  RopaCsvNameLookup,
  ropaEntriesToCsv,
  ropaEntryToCsvRows,
} from "./ropa.csv";
import { RopaEntryResponse } from "./ropa.types";

// PrivacyCare — Screen 3 of docs/design/privacycare-screens/DESIGN.md.
// Real fuel-retailer data — "Fuel Card Issuance" (Card Operations, register
// reference 17) and "CSR Planning & Execution" (CSR) are the exact two
// business processes the build prompt names.
const FUEL_CARD_PROCESS: RopaEntryResponse["process"] = {
  id: "bp_9e332dcbe707",
  name: "Fuel Card Issuance",
  description: null,
  business_cycle: "Card Operations",
  owner_name: "Josephine Wanjiru",
  owner_email: "josephine@example.co.ke",
  is_critical: true,
  criticality_note: null,
  external_ref: "17",
  last_attested_at: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-10T00:00:00Z",
};

const CSR_PROCESS: RopaEntryResponse["process"] = {
  id: "bp_csr_planning",
  name: "CSR Planning & Execution",
  description: null,
  business_cycle: "CSR",
  owner_name: null,
  owner_email: null,
  is_critical: false,
  criticality_note: null,
  external_ref: "42",
  last_attested_at: null,
  created_at: null,
  updated_at: null,
};

const NAMES: RopaCsvNameLookup = {
  dataUseName: (key) =>
    ({ "essential.service.kyc": "KYC Verification" })[key] ?? key,
  dataCategoryName: (key) =>
    ({
      "user.demographic.religious_belief": "Religion",
      "user.financial": "Financial Information",
    })[key] ?? key,
  dataSubjectName: (key) => ({ customer: "Customer" })[key] ?? key,
};

describe("ropa.csv — column shape", () => {
  it("matches the Title Case, space-separated header convention read from Fides' own OSS CSV export", () => {
    expect(ROPA_CSV_FIELDS).toEqual([
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
    ]);
  });

  it("names the filename in the download_{date}.csv shape, per process and register", () => {
    expect(ropaCsvFilename("process")).toMatch(
      /^ropa_process_download_\d{4}-\d{2}-\d{2}\.csv$/,
    );
    expect(ropaCsvFilename("register")).toMatch(
      /^ropa_register_download_\d{4}-\d{2}-\d{2}\.csv$/,
    );
  });
});

describe("ropa.csv — one row per processing activity, process columns repeated", () => {
  it("repeats the process's own columns on every activity row", () => {
    const entry: RopaEntryResponse = {
      process: FUEL_CARD_PROCESS,
      declarations: [
        {
          id: "pd_1",
          name: "Fuel Card Issuance — KYC verification",
          data_use: "essential.service.kyc",
          data_categories: [
            "user.demographic.religious_belief",
            "user.financial",
          ],
          data_subjects: ["customer"],
          legal_basis: "Legitimate interests",
          retention_period: "7 years post account closure",
          system_id: "sys_1",
          system_name: "Fuel Card Platform",
        },
        {
          id: "pd_2",
          name: "Fuel Card Issuance — Statement dispatch",
          data_use: "essential.service.kyc",
          data_categories: ["user.financial"],
          data_subjects: ["customer"],
          legal_basis: "Legitimate interests",
          retention_period: "7 years post account closure",
          system_id: "sys_1",
          system_name: "Fuel Card Platform",
        },
      ],
      missing_declarations: [],
    };

    const rows = ropaEntryToCsvRows(entry, NAMES);

    expect(rows).toHaveLength(2);
    rows.forEach((row) => {
      expect(row[0]).toBe("Fuel Card Issuance");
      expect(row[1]).toBe("Card Operations");
      expect(row[2]).toBe("17");
      expect(row[3]).toBe("Josephine Wanjiru");
    });
    // Human names, not raw taxonomy keys, in the exported file too.
    expect(rows[0][6]).toBe("Religion; Financial Information");
    expect(rows[0][5]).toBe("KYC Verification");
    expect(rows[0][8]).toBe("Legitimate interests");
    expect(rows[0][9]).toBe("7 years post account closure");
    expect(rows[1][4]).toBe("Fuel Card Issuance — Statement dispatch");
  });

  it("records a missing_declaration as a named row, never drops it", () => {
    const entry: RopaEntryResponse = {
      process: CSR_PROCESS,
      declarations: [],
      missing_declarations: ["pd_gone_1"],
    };

    const rows = ropaEntryToCsvRows(entry, NAMES);

    expect(rows).toHaveLength(1);
    expect(rows[0][0]).toBe("CSR Planning & Execution");
    expect(rows[0][ROPA_CSV_FIELDS.length - 1]).toContain("pd_gone_1");
    expect(rows[0][ROPA_CSV_FIELDS.length - 1]).toContain("no longer exists");
  });

  it("gives a process with no activities exactly one informative row, never zero rows", () => {
    const entry: RopaEntryResponse = {
      process: CSR_PROCESS,
      declarations: [],
      missing_declarations: [],
    };

    const rows = ropaEntryToCsvRows(entry, NAMES);

    expect(rows).toHaveLength(1);
    expect(rows[0][0]).toBe("CSR Planning & Execution");
    expect(rows[0][ROPA_CSV_FIELDS.length - 1]).toMatch(
      /no processing activity has been recorded/i,
    );
  });
});

describe("ropa.csv — a whole-register export", () => {
  it("produces a CSV with a header row and one row per activity across every process, in the same shape as a single-entry export", () => {
    const entries: RopaEntryResponse[] = [
      {
        process: FUEL_CARD_PROCESS,
        declarations: [
          {
            id: "pd_1",
            name: "Fuel Card Issuance — KYC verification",
            data_use: "essential.service.kyc",
            data_categories: ["user.financial"],
            data_subjects: ["customer"],
            legal_basis: "Legitimate interests",
            retention_period: "7 years post account closure",
            system_id: "sys_1",
            system_name: "Fuel Card Platform",
          },
        ],
        missing_declarations: [],
      },
      {
        process: CSR_PROCESS,
        declarations: [],
        missing_declarations: [],
      },
    ];

    const csv = ropaEntriesToCsv(entries, NAMES);
    const lines = csv.trim().split(/\r\n|\n/);

    // Header + one activity row for Fuel Card Issuance + one informative
    // row for CSR Planning & Execution (no activities recorded).
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe(ROPA_CSV_FIELDS.join(","));
    expect(lines[1]).toContain("Fuel Card Issuance");
    expect(lines[2]).toContain("CSR Planning & Execution");
    expect(lines[2]).toContain("No processing activity has been recorded");
  });
});
