#!/usr/bin/env python3
"""Seed the PrivacyCare DPIA-spine demo data (spec 2026-09-18, "PrivacyCare
demo seed — design").

**THIS IS NOT THE CUSTOMER'S DATA.** Josephine has screened nothing herself,
has no risk assessment and has filed no DSR. Everything this script writes
is authored by us, grounded in her REAL loaded vocabularies (87 business
processes, 48 data subjects, 114 data categories, 56 data uses, 23 lawful-
basis grounds of which 11 carry a derived legal basis), never inventing her
content. The design doc spends most of its rules on making that distinction
survive contact with a demo, and this docstring repeats the load-bearing
ones.

WHAT THIS TASK SEEDS. Two things, both "the DPIA spine minus generation":

1. Screening decisions on 38 of her 87 business processes (23 applicable,
   15 not applicable), chosen to spread across business cycles rather than
   cluster, so the cycle filter on the screening table is demonstrable.
   Combined with the 2 pre-existing demonstration decisions left in place
   by plan 20 Task 5 (Fuel Card Issuance — applicable; CSR Planning &
   Execution — not applicable), the register reads 40 decided (24
   applicable / 16 not applicable) and 47 still "Not screened" — a
   mid-programme state, not a fully triaged register, which is the whole
   point (D-SEED-4): a register where every process has already been
   triaged hides the very screen that does the triaging.

2. A data mapping — data subjects, data categories, lawful-basis ground,
   purpose — for each of the 24 applicable processes that does not already
   have one. 23 of the 24 need a fresh mapping; the 24th (Fuel Card
   Issuance) was already mapped by plan 20 Task 5 and is left untouched.

WHAT THIS TASK DELIBERATELY DOES NOT SEED (D-SEED-6, out of scope here on
the CEO's explicit instruction — real generation costs real LLM spend and
this script is reviewed before that spend happens): no privacy_assessment
rows, no privacycare_dpia_risk rows beyond the 3 already in place. A later
task drives the existing generation route against the 24 applicable,
mapped processes this script produces.

THE MARKER, AND WHY IT IS TWO DIFFERENT SHAPES ON TWO DIFFERENT TABLES.
Prior art already exists for exactly this problem: the mapping route
(screening/mapping.py) tags everything IT creates with the literal string
'privacycare:mapping_route', in `privacydeclaration.features` (an
ARRAY(String) column) and in `ctl_systems.tags` (the same shape). This
script follows that SAME MECHANISM — a marker string living in a free-text
array column the table already has, no migration — but with ITS OWN marker,
DEMO_FEATURE_MARKER = 'privacycare:demo_seed', so that this script's own
`--remove` can never touch the 4 processing activities / 3 systems / 5
links / 2 screening decisions / 3 risks the demo-rows inventory
(docs/demo/privacycare-demo-rows.md) already documents and asks not to be
disturbed. Those rows carry 'privacycare:mapping_route' and nothing else;
this script's rows carry 'privacycare:demo_seed' ADDED ALONGSIDE whatever
save_mapping() itself already wrote — see "WHY THE MARKER IS APPENDED, NOT
SUBSTITUTED" below for why the mapping_route marker is deliberately left in
place rather than overwritten.

privacycare_screening_decision has no array column at all (see
54c8fce54023_screening.py: id, business_process_id, dpia_required,
triggered_keys, justification, decided_by, decided_at) and this task adds
NO migration to give it one. justification is the one free-text field, but
it is unusable as a marker location: the table's own CheckConstraint
(ck_screening_screenout_has_a_reason) forces justification to be NULL on
every applicable (dpia_required=true) row, so a marker parked there would
be silently absent from exactly the applicable half of this seed's own
rows. decided_by is the field this script actually uses: it is a free-text
`character varying`, present and NOT NULL on every row regardless of
verdict, and it is precisely the field the product's own UI documents as
"the audit record — who decided this, on this date" (api/screening.py's
own module comment). This script writes
DECIDED_BY_MARKER = 'client:privacycare:demo_seed' — not the bare marker,
but the SAME "client:<identity>" shape identity.py's own
_created_by_from_client already writes for a root/admin login or a bare
M2M client with no linked FidesUser. screening.py's own
_decided_by_display() recognises that exact prefix and resolves it to the
plain-language label "System (automated) — not a named user" — an HONEST
read for a screen decided by a seed script, and a much better one than the
alternative: a bare, non-"client:"-prefixed marker string would instead hit
_decided_by_display()'s case 3 (a value that looks like a user id but
resolves to no fidesuser row) and show "This user's account is no longer
available", which is not what happened and would read to Carol as a
data-integrity problem in a compliance artifact, not as demo content. The
'privacycare:demo_seed' marker is embedded, findable, VERBATIM inside that
identifier — `decided_by = 'client:privacycare:demo_seed'` is an EXACT
match this script's own --remove keys off (see remove_demo() below), not a
substring search.

WHY THE MARKER IS APPENDED, NOT SUBSTITUTED (idempotency, not honesty
theatre). save_mapping()'s own idempotency (mapping.py's
_EXISTING_ROUTE_ACTIVITY_SQL) keys ONLY on whether an activity carries
MAPPING_ROUTE_FEATURE_MARKER ('privacycare:mapping_route') — that is code
this task reuses rather than reimplements (see "GOES THROUGH THE PRODUCT'S
OWN CODE PATH" below), and it is not parameterised on a caller-supplied
marker. If this script instead REPLACED that marker with its own on every
row it touched, a second run would find no mapping_route-tagged activity
for the process (because the first run's own append-and-swap had erased
it), take save_mapping()'s CREATE branch again, and silently double every
mapping — the exact "must not double anything" failure D-SEED-1 forbids.
So seed_mappings() lets save_mapping() write its own marker exactly as
designed, then APPENDS 'privacycare:demo_seed' alongside it
(`array_append(features, :marker) WHERE NOT (:marker = ANY(features))`) —
both markers coexist on every row this script creates. save_mapping()'s own
idempotency keeps working unmodified on any rerun; this script's OWN
idempotency (seed_mappings()'s _EXISTING_DEMO_MAPPING_SQL, keyed on the
demo marker alone) short-circuits before save_mapping() is even called a
second time, so in practice save_mapping() only ever runs once per process
under this script. Prior-art rows (the 2 mapping_route-only activities/
systems from plan 20 Task 5) are NEVER touched by this append — this
script only appends to a row it is in the middle of creating or one its own
_EXISTING_DEMO_MAPPING_SQL check has already excluded from re-processing —
so they permanently carry mapping_route alone, and this script's
marker-scoped SELECTs (remove_demo(), see below) never see them.

GOES THROUGH THE PRODUCT'S OWN CODE PATH, PER THE BRIEF. "A seed that
writes rows the product would not have written is a seed that demos
something we do not ship." Screening decisions are written through
screening/gate.py's `record_decision()` — the exact function
POST /api/v1/privacycare/screening already calls, which derives
dpia_required from triggered_keys (never accepted as a separate argument),
enforces the screen-out-needs-a-reason rule, and validates trigger keys by
name. Data mappings are written through screening/mapping.py's
`save_mapping()` — the exact function the mapping route already calls,
which creates the privacydeclaration, links it via
privacycare_process_declaration, provisions (or reuses) a ctl_systems row,
derives legal_basis_for_processing from the chosen ground via
privacycare_processing_ground.fides_legal_basis, records that ground's
provenance via grounds.py's `_record_declaration_ground`, and derives
processes_special_category_data from the chosen data_categories against
the SAME ancestor-prefix tag walk the read path uses. Nothing in this
script computes any of those five things itself.

THE ELEVEN USABLE GROUNDS, NOT THE OTHER TWELVE (D-SEED-3, OQ-W2-4). Of the
23 rows in privacycare_processing_ground, 11 carry a non-null
fides_legal_basis and 12 do not — Carol has not yet ruled on the other 12,
and save_mapping() itself refuses to derive a legal basis from one that
has none (raises ValueError, "no legal basis has been determined yet for
ground ..."). Every `ground` value in MAPPINGS below is one of the 11
usable ones, verified against the live table before this docstring was
written (Adherence to Pension / Collective Agreement Laws, Background
Checks and Pre-employment Screening, Compliance with Measures / Laws to
Redress Unfair Discrimination, Consent by a Child's Parent or Guardian,
Consent by the Data Subject, Customer Relationship Administration,
Enrolment of an Applicant, KYC Requirements, Labour Legislation
Compliance, Marketing, Obligation of Law (SPI)). This seed does not work
around the gap; it demonstrates the product's honest behaviour at the
boundary of it, exactly as D-SEED-3 asks.

WHY THESE 38 PROCESSES, NOT A RANDOM 38. Business processes were selected
by hand, one at a time, for a PLAUSIBLE personal-data story: Payroll
Administration handles employee bank details and KRA PINs under Labour
Legislation Compliance; Medical Records Filing handles health data under
Obligation of Law (SPI); Recruitment Management handles candidate CVs
under Enrolment of an Applicant; Accounts Payable & Receivable handles
corporate vendor payments with no individual-scale personal data at all
and is marked not applicable. "Getting this plausible is most of the
demo's credibility" (the brief's own words) is why SCREENING_DECISIONS and
MAPPINGS below are long, explicit, hand-authored tables rather than a
generator — a script that invented plausible-sounding content
programmatically would be exactly the kind of synthesis this repo's
CLAUDE.md forbids for CVEQ canon, and the same discipline applies here to
a compliance artifact.

Every one of the 15 NOT-APPLICABLE justifications below names all SIX
screening triggers by their exact seeded label (Large-scale processing;
Special category or highly sensitive data; Systematic monitoring; New or
unproven technology; Automated decision-making with legal or similarly
significant effect; Processing involving vulnerable data subjects) and
argues, in process-specific terms, why each does not apply — never a bare
"no personal data is involved" (the demo-rows inventory itself documents an
EARLIER version of the CSR justification that made exactly that mistake
while its own mapping recorded contact details; this task does not repeat
it). Every justification also says, in its own words, that it is
demonstration content written by this seed script — the CEO's own words:
"this is not the customer's data and must never be mistaken for it."

RECORD_DECISION IS APPEND-ONLY, WHICH IS WHAT MAKES SCREENING IDEMPOTENT.
gate.py's own docstring: "NEVER updates: re-screening appends... There is
no update path here at all to accidentally call." seed_screening_decisions()
therefore keys its OWN idempotency on `gate.current_verdict(db, bp_id) is
None` — if a process already has ANY decision (ours from an earlier run of
this script, or anyone else's), this script skips it rather than appending
a second, contradictory one. None of the 38 processes below already carries
a decision (verified against the live database); the two pre-existing
demonstration decisions (Fuel Card Issuance, CSR Planning & Execution) are
therefore untouched BY CONSTRUCTION — they are simply never named in
SCREENING_DECISIONS.

Same shape as scripts/privacycare/seed_dsr.py, seed_kenya_template.py,
seed_consent_demo.py and seed_screening_triggers.py: one argparse CLI, one
session, dry-run by default, --commit to persist. This script ADDS a third
mode, --remove, that seed_dsr.py's siblings do not have — the brief asks
for a genuine removal path ("a seed you cannot remove is a seed that
contaminates the customer's environment permanently"), which none of the
earlier PrivacyCare seeds needed because none of them wrote rows onto a
table the customer's own real data lives beside. --remove follows the SAME
dry-run-by-default rule as seeding: `--remove` alone reports what WOULD be
deleted and rolls back; `--remove --commit` actually deletes.
_database_url() and _target_description() are copied verbatim from
seed_dsr.py (itself copied from seed_connection.py, itself copied from
import_processes.py, itself copied from migrations/env.py) rather than
imported — see load_taxonomy.py's module docstring for why importing
env.py isn't safe here (it runs Alembic migrations as a side effect of the
import itself).

Secrets come from the environment at run time and are never written into
the repo or printed — main() only ever prints the target: line (host/
port/database, never the password, never the raw URL).

SCREENING AND MAPPING ARE RAW SQL; DSR AND CONSENT ARE NOT — SO main() DOES
CARRY A COMMIT-TRAP GUARD AFTER ALL. gate.record_decision and
mapping.save_mapping (and this script's own append-marker / --remove
statements for those two tables) are all raw SQL through the session,
exactly like gate.py's, mapping.py's and grounds.py's own module
docstrings promise. The DSR and consent additions below are not: DSR
requests go through dsr/register.py's record_request (also raw SQL — no
trap there), but consent goes through seed_consent_demo.py's own
seed_consent_demo() AND this script's own new preference rows, both of
which call PrivacyPreferenceHistory.create() — Fides' base_class.
persist_obj, which does add/commit/refresh UNCONDITIONALLY (see
seed_consent_demo.py's own module docstring, "The commit trap"). So
main() below now carries the exact same monkeypatch-commit-to-flush guard
seed_dsr.py's and seed_consent_demo.py's own main() already use, for the
whole seed/remove call — not only the consent portion — matching the
pattern the `db` test fixture in test_seed_demo.py already applied
defensively even before this was true ("in case a future edit adds an ORM
write"). It is now load-bearing, not defensive.

Every write function below (seed_screening_decisions, seed_mappings,
seed_dsr_requests, seed_consent, seed_demo, remove_demo) never calls
db.commit() or db.rollback() itself — the caller's session boundary
decides, the same rule every PrivacyCare seed function in this package
follows. (seed_consent's own inner call into seed_consent_demo() and its
own PrivacyPreferenceHistory.create() calls DO call persist_obj's
commit — but under main()'s guard that resolves to a flush, same as
seed_consent_demo.py's own main() already relies on.)

--- D-SEED-8: DSR REQUESTS THAT EXERCISE THE KENYAN CLOCKS ---------------

seed_dsr.py already seeds the six timeline rows and the three Kenyan
Fides policies (plan 14's runtime prerequisites) — but seeds ZERO
`privacycare_dsr_request` rows; the design doc's own baseline table says
so directly ("DSR requests | 0"). This script REUSES, not duplicates,
exactly the one piece of that prerequisite it actually needs:
`dsr.timelines.seed_timelines(db)` (idempotent, ON CONFLICT DO NOTHING),
called at the top of seed_dsr_requests() below, the same function
seed_dsr.py itself calls. It deliberately does NOT call
`dsr.delegation.ensure_kenyan_policies()` or `dsr.delegation.delegate()`
— the live database already carries all three Kenyan policies (verified
directly against it before writing this), but delegating would create a
real Fides `privacyrequest` row per delegating right, which is a
different capability (Fides executing the DSR) from the one D-SEED-8
actually asks for (the register's OWN clocks and alerting having
something true to compute against). Every request this script writes
stays an open register row, exactly as `dsr.register.record_request`
leaves it — never delegated, never decided.

Seven requests, one per Kenyan right plus a second `access` request, are
written through `dsr.register.record_request()` — the exact function
`POST /api/v1/privacycare/dsr-requests` calls — with `received_at`
backdated by a fixed number of days from the moment this script runs, so
that `deadline_at` (computed and stored by record_request itself, per
dsr/timelines.py's `deadline_for`) lands in a different place relative to
`dsr/alerts.py`'s own `alert_due`/`warn_threshold` thresholds for each
row:

  access (7 days, warn threshold ceil(7/3)=3 days): 1 day old (6 days
  left — comfortably inside) and 5 days old (2 days left — close to
  breach, inside the warn threshold).
  rectification (14 days, threshold 5): 10 days old (4 days left — close
  to breach).
  erasure (14 days, threshold 5): 17 days old (3 days OVERDUE — deadline
  already passed).
  restriction (14 days, threshold 5): 9 days old (5 days left — exactly
  AT the warn threshold, a deliberate boundary case for "close to
  breach").
  portability (30 days, threshold 10): 5 days old (25 days left —
  comfortably inside).
  objection (unclocked — dsr/timelines.py's own `_TIMELINES` stores
  `days=None` for it, OQ-PRIVACY-02 still open): received today.
  `deadline_at` is NULL for this row regardless of its age, by
  construction (deadline_for(received_at, None) is None) — the one
  request D-SEED-8 explicitly asks for, to prove the unclocked case
  renders as "no deadline", never a false one.

This spread is measured against `dsr/alerts.py`'s REAL threshold function
(warn_threshold = ceil(days_allowed / 3)), not guessed — the same
discipline D-SEED-9 applies to the consent detector below, applied here
first. Ages are computed relative to `datetime.now(timezone.utc)` AT THE
MOMENT THE SCRIPT RUNS, not a hardcoded calendar date, so "close to
breach" reads as close to breach on the day this seed is actually
committed — exactly the design doc's own framing ("requests spread across
those states... so the alerting has something true to say" is a
statement about the day of the demo, not a permanent invariant a seed
written months from now could still satisfy with the same fixed dates).

REALISTIC KENYAN IDENTITIES, MARKED AS SYNTHETIC IN THE DATA ITSELF, NOT
ONLY IN THIS DOCSTRING. `privacycare_dsr_request` (migration
ff91bc4d23d9_dsr_register.py) has exactly one free-text column with no
CHECK constraint and no NOT-NULL-with-a-real-meaning conflict:
`subject_identifier` (`character varying(255)`, NOT NULL, the requester's
own identity — the same field the mapping/screening tables' `features`/
`tags`/`decided_by` play the marker role for, but a scalar string here,
so the marker is APPENDED as a bracketed suffix, never substituted for
the name). `owner_email` was ruled out for the same reason seed_demo.py's
own docstring already rules out `decided_by` for a marker on the wrong
table: it is read by alert_job.py as a real delivery address
(`run_deadline_alerts`'s `recipient = row["owner_email"]`), and a marker
string sitting there would misroute — or worse, silently "succeed" as a
delivery target — an alert that is supposed to demonstrate the unowned/
escalation path. `_dsr_subject_identifier()` below renders
`"{name} — {contact} [privacycare:demo_seed — synthetic demo requester,
not a real data subject]"` — a full Kenyan name and a phone number or
`.invalid`-domain email (RFC 2606, the same reserved-for-fake-addresses
convention DEMO_SUBJECT_EMAIL in seed_consent_demo.py already
establishes) PLUS the same DEMO_FEATURE_MARKER this script already writes
elsewhere, so a consultant reading the register row directly — not a
design doc, not a code comment — sees both that Grace Wanjiru looks like
a real Kenyan fuel-card customer AND that she explicitly is not one.
DSR_REQUESTS below never uses "Test User", "foo", or placeholder text —
every name, phone/email and one-line scenario (in DSR_REQUESTS' own
`role_note`, documentation only, never written to the database) is
purpose-written for a Kenyan petroleum marketer's actual customer and
staff population, the same discipline SCREENING_DECISIONS' justifications
already apply.

Five of the seven are linked to one of Josephine's REAL business
processes (via `_find_business_process_id`, already defined below,
reused rather than re-implemented) — Fuel card application processing,
Customer Loyalty Program Management, Employee Records Management, Driver
& Transporter Records, M-Pesa Integration & Settlement, Digital Marketing
Analytics — so `dsr.register.resolve_owner`'s
`business_process`-sourced branch of D-DSR-8's fallback chain is
demonstrable too, D-SEED-3 style (grounded in her real vocabularies).
The erasure request (Peter Kamau) is deliberately left unlinked, with no
explicit `owner_email` either, to demonstrate the OTHER end of that same
chain — `configured_dpo` or `unassigned`, depending on whether
`PRIVACYCARE_DPO_EMAIL` is set in the environment this script runs
against — the "nobody owns this yet" state `alert_job.py`'s own
`unowned` counter and escalation path exist to catch.

Idempotent the same way seed_mappings() already is: keyed on an EXACT
match against the full, deterministic `subject_identifier` string this
script itself constructs for each DSR_REQUESTS entry (not the marker
alone, which is shared across all seven and cannot distinguish them) —
`_EXISTING_DEMO_DSR_REQUEST_SQL` below. A second call finds all seven and
writes nothing.

--- D-SEED-9: CONSENT THE STALE-CONSENT DETECTOR CAN ACTUALLY ACT ON -----

consent/detector.py's own module docstring is unambiguous, and this
script is written against what it actually says, not against the word
"stale" taken at face value: `find_stale_consents` computes staleness
from VERSION LAG, never from elapsed time — a subject's current
`opt_in` position is stale exactly when the `privacynoticehistory`
version they consented against is missing a `data_use` the LIVE version
now carries (`consent/materiality.py`'s `is_materially_different` /
`added_uses`, rule `gained_data_use`). There is no calendar threshold
anywhere in that code path to guess at. So "fresh" here means "consented
against the CURRENT live version" and "stale" means "consented against an
EARLIER version that the live one has since outgrown" — not "consented
N days ago" — and `received_at` on each new preference row below is set
for narrative realism only (a demo viewer reading the finding's own
timestamp), never because the detector reads it for this purpose (it
does surface `received_at` on a StaleConsent result, but only as
reported detail, never as a filter — see detector.py's own `StaleConsent`
dataclass comment).

REUSES, DOES NOT DUPLICATE, seed_consent_demo.py's own notice. This
script does not create a second notice, a third history version, or its
own copy of the encrypted-write dance — `seed_consent()` below loads
scripts/privacycare/seed_consent_demo.py the same way
tests/privacycare/test_seed_consent_demo.py and test_seed_demo.py already
load a script under this package (which carries no `__init__.py`, so it
is not an importable package — `importlib.util.spec_from_file_location`
is the only way in), and calls its PUBLIC `seed_consent_demo(db)`
directly. That call is itself idempotent and ensures three things exist
before this script adds anything of its own: the one active
`privacycare_consent_rule` row, the `privacycare_demo_fuel_card_marketing`
notice, and both its history versions (v1: `marketing.advertising`
only; v2 — the LIVE version — adds `marketing.advertising.third_party`).
Its own pre-existing preference row (DEMO_SUBJECT_EMAIL, opted in at v1)
is left exactly as seed_consent_demo.py made it; this script never
selects, updates or deletes it — see "REMOVAL NEVER TOUCHES
SEED_CONSENT_DEMO.PY'S OWN ROW" below.

Two NEW preference rows are added, both through
`PrivacyPreferenceHistory.create()` — never raw SQL — for the identical
reason seed_consent_demo.py's own docstring gives ("The encryption trap"):
`email` is a `StringEncryptedType` column, and only the ORM's
create/persist_obj path actually encrypts it at rest; a raw INSERT would
write plaintext where the decrypting type decorator expects ciphertext,
and detector.py's own ORM-based identity read (necessary for the same
reason) would then fail to recover it. One row (`alice.wambui...`,
opt_in) is written against v2, the live version — FRESH, and provably
excluded from `find_stale_consents`'s report, since `added_uses(v2, v2)`
is empty. One row (`kevin.mutiso...`, opt_in) is written against v1 —
STALE by the exact same version-lag test the pre-existing demo subject
already demonstrates, giving the demo TWO stale findings instead of one
and proving the detector's judgment is about the version gap, not about
which particular subject happens to be seeded.

THE MARKER, ON THE ONE UNENCRYPTED FREE-TEXT COLUMN THIS TABLE HAS.
`privacypreferencehistory.url_recorded` (`ConsentReportingMixinV2`,
`fides.api.models.privacy_preference`) is a plain `String` column — NOT
one of the four `StringEncryptedType` identity columns
(`ConsentIdentitiesMixin`) detector.py's own docstring warns raw SQL away
from — and it plays no role `find_stale_consents`'s `StaleConsent` result
ever surfaces (that dataclass reports `subject`/`subject_kind`/
`notice_key`/`notice_name`/version numbers/`added_uses`/`preference`/
`received_at` — never `url_recorded`), so writing DEMO_FEATURE_MARKER
into it costs the demo nothing a viewer of the detector's OWN report
would ever see, while giving `remove_demo()` an exact-match column to key
its DELETE on: `WHERE url_recorded = :marker`. This script's two new rows
carry it; seed_consent_demo.py's own pre-existing row does not (its own
`PrivacyPreferenceHistory.create()` call never sets `url_recorded`, so it
reads NULL) — the one fact `remove_demo()`'s own SELECT depends on to
leave that row alone. Idempotent the same way DSR requests are: keyed on
an exact `(url_recorded, privacy_notice_history_id)` match, since the two
new rows point at different history versions (v1 vs v2) and that pair is
fully deterministic per entry.

REMOVAL NEVER TOUCHES SEED_CONSENT_DEMO.PY'S OWN ROW, ITS NOTICE, ITS
VERSIONS, OR ITS RULE ROW. remove_demo()'s new DELETEs are scoped to
`privacycare_dsr_request` (by `subject_identifier LIKE
'%privacycare:demo_seed%'`) and `privacypreferencehistory` (by
`url_recorded = 'privacycare:demo_seed'`) ONLY — exactly the two tables
this task's own two new write functions create rows in. `privacynotice`,
`noticetranslation`, `privacynoticehistory` and `privacycare_consent_rule`
are never selected, updated or deleted by this script at all, in either
mode: they are seed_consent_demo.py's own tables to own, and this task's
job is to ADD demonstrable preference rows against what it already built,
not to take over its removal path too.
"""
import argparse
import importlib.util
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import sqlalchemy
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import fides.api.db.base  # noqa: F401 — see below
from fides.api.models.privacy_preference import PrivacyPreferenceHistory
from fides.api.privacycare.dsr import register as dsr_register
from fides.api.privacycare.dsr.timelines import seed_timelines
from fides.api.privacycare.screening import gate
from fides.api.privacycare.screening import mapping as mapping_module

# `fides.api.db.base` is imported (but not from) purely for its side effect:
# it is Alembic's single central import point that pulls in every mapped
# SQLAlchemy class before any of them are used — the same reason seed_dsr.py
# and seed_consent_demo.py carry this import. mapping.save_mapping touches
# System/PrivacyDeclaration-adjacent relationships indirectly through raw
# SQL joins, and this import removes any risk of a mapper-configuration
# ordering failure the first time those relationships are touched.
# PrivacyPreferenceHistory is imported directly (not only reached through
# seed_consent_demo.py's loaded module) because this script's own two new
# preference rows (see "D-SEED-9" above) are written through it too.

# --- The marker -------------------------------------------------------------

# Written into privacydeclaration.features and ctl_systems.tags, ALONGSIDE
# whatever mapping.save_mapping() itself already writes there (see this
# module's own docstring, "WHY THE MARKER IS APPENDED, NOT SUBSTITUTED") —
# never in place of it. The ONE thing remove_demo() keys off of for those
# two tables.
DEMO_FEATURE_MARKER = "privacycare:demo_seed"

# Written into privacycare_screening_decision.decided_by AND
# privacycare_declaration_ground.recorded_by — see this module's own
# docstring, "THE MARKER, AND WHY IT IS TWO DIFFERENT SHAPES ON TWO
# DIFFERENT TABLES", for why this is 'client:<marker>' rather than the bare
# marker (screening.py's own _decided_by_display() resolves the
# "client:..." prefix to an honest "System (automated)" label instead of a
# misleading "this user's account is no longer available" one). The ONE
# thing remove_demo() keys off of for privacycare_screening_decision.
DECIDED_BY_MARKER = f"client:{DEMO_FEATURE_MARKER}"


# --- Screening decisions: 23 applicable, 15 not applicable -----------------
#
# Each entry: (process name — must match privacycare_business_process.name
# exactly, business cycle — for this module's own documentation only, never
# read by any query below, applicable, triggered trigger_keys — empty for a
# not-applicable decision, justification — None for an applicable decision,
# required for a not-applicable one).
#
# Every justification below names all six seeded trigger LABELS verbatim
# (test_seed_demo.py asserts this against the live privacycare_screening_
# trigger table, not against a copy typed out a second time in the test —
# see that test's own comment for why) and closes with an explicit
# self-disclosure that this is demonstration content, never the customer's
# own reasoning.
_DEMO_DISCLOSURE = (
    "Demonstration content written by the seed script "
    "(privacycare:demo_seed) for the PrivacyCare demo; not a decision "
    "Josephine or Carol has made."
)

SCREENING_DECISIONS: tuple[dict, ...] = (
    # --- Finance & Accounting (pool 11; 4 applicable, 2 not applicable) ----
    {
        "process": 'Payroll Administration',
        "cycle": 'Finance & Accounting',
        "applicable": True,
        "triggers": ('large_scale',),
        "justification": None,
    },
    {
        "process": 'M-Pesa Integration & Settlement',
        "cycle": 'Finance & Accounting',
        "applicable": True,
        "triggers": ('large_scale', 'systematic_monitoring'),
        "justification": None,
    },
    {
        "process": 'Insurance Claims Processing',
        "cycle": 'Finance & Accounting',
        "applicable": True,
        "triggers": ('special_category',),
        "justification": None,
    },
    {
        "process": 'OIl Distributor Wallet Transaction Monitoring',
        "cycle": 'Finance & Accounting',
        "applicable": True,
        "triggers": ('large_scale', 'systematic_monitoring'),
        "justification": None,
    },
    {
        "process": 'Accounts Payable & Receivable',
        "cycle": 'Finance & Accounting',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Accounts Payable & Receivable exchanges invoices, purchase "
            "orders and settlement instructions with the marketer's "
            "corporate vendors and business customers. The bank account and "
            "contact details recorded belong to companies and their "
            "designated finance contacts, a small, named population rather "
            "than a broad set of individuals, and are used only to process "
            "routine trade payments. It triggers none of the six DPIA "
            "screening questions: Large-scale processing, Special category "
            "or highly sensitive data, Systematic monitoring, New or "
            "unproven technology, Automated decision-making with legal or "
            "similarly significant effect, and Processing involving "
            "vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    {
        "process": 'Fixed Asset Register Updates',
        "cycle": 'Finance & Accounting',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Fixed Asset Register Updates records the acquisition, location "
            "and depreciation of company property — fuel pumps, depot "
            "equipment, vehicles — against an internal asset register. The "
            "only personal data involved is the name of the staff member "
            "who last verified an item's condition, a small and stable set "
            "of internal custodians. It triggers none of the six DPIA "
            "screening questions: Large-scale processing, Special category "
            "or highly sensitive data, Systematic monitoring, New or "
            "unproven technology, Automated decision-making with legal or "
            "similarly significant effect, and Processing involving "
            "vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Logistics & Supply Chain (pool 8; 2 applicable, 1 not applicable)-
    {
        "process": 'Driver & Transporter Records',
        "cycle": 'Logistics & Supply Chain',
        "applicable": True,
        "triggers": ('large_scale',),
        "justification": None,
    },
    {
        "process": 'Fleet Movement Logs',
        "cycle": 'Logistics & Supply Chain',
        "applicable": True,
        "triggers": ('systematic_monitoring',),
        "justification": None,
    },
    {
        "process": 'Depot Inventory Tracking',
        "cycle": 'Logistics & Supply Chain',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Depot Inventory Tracking records stock-keeping units, volumes "
            "and reorder points for fuel and lubricants held at each depot. "
            "The records are about product, not people — the only personal "
            "reference is the depot supervisor's name against a stock "
            "count, incidental to the process rather than its purpose. It "
            "triggers none of the six DPIA screening questions: Large-scale "
            "processing, Special category or highly sensitive data, "
            "Systematic monitoring, New or unproven technology, Automated "
            "decision-making with legal or similarly significant effect, "
            "and Processing involving vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Human Resources (pool 8; 3 applicable, 1 not applicable) ---------
    {
        "process": 'Medical Records Filing',
        "cycle": 'Human Resources',
        "applicable": True,
        "triggers": ('special_category',),
        "justification": None,
    },
    {
        "process": 'Recruitment Management',
        "cycle": 'Human Resources',
        "applicable": True,
        "triggers": ('large_scale', 'automated_decision'),
        "justification": None,
    },
    {
        "process": 'Employee Records Management',
        "cycle": 'Human Resources',
        "applicable": True,
        "triggers": ('special_category', 'large_scale'),
        "justification": None,
    },
    {
        "process": 'Training & Development Tracking',
        "cycle": 'Human Resources',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Training & Development Tracking records which staff have "
            "completed which courses and certifications, against a modest "
            "annual training calendar. It uses employee name and course "
            "completion status only, drawn from records already held for "
            "Employee Records Management, and produces no profile beyond a "
            "pass/fail attendance log. It triggers none of the six DPIA "
            "screening questions: Large-scale processing, Special category "
            "or highly sensitive data, Systematic monitoring, New or "
            "unproven technology, Automated decision-making with legal or "
            "similarly significant effect, and Processing involving "
            "vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Customer Service (pool 7; 2 applicable, 1 not applicable) --------
    {
        "process": 'KYC Documentation Collection (Customer)',
        "cycle": 'Customer Service',
        "applicable": True,
        "triggers": ('large_scale',),
        "justification": None,
    },
    {
        "process": 'Customer Call Center Operations',
        "cycle": 'Customer Service',
        "applicable": True,
        "triggers": ('systematic_monitoring',),
        "justification": None,
    },
    {
        "process": 'Customer Feedback Collection',
        "cycle": 'Customer Service',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Customer Feedback Collection gathers voluntary comments "
            "submitted through the station suggestion box and the customer "
            "care email address, logged with a name and comment only when "
            "the customer chooses to give one. Volumes are low, submission "
            "is entirely optional, and nothing is profiled or scored. It "
            "triggers none of the six DPIA screening questions: Large-scale "
            "processing, Special category or highly sensitive data, "
            "Systematic monitoring, New or unproven technology, Automated "
            "decision-making with legal or similarly significant effect, "
            "and Processing involving vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Marketing & Communications (pool 7; 2 app, 1 not applicable) -----
    {
        "process": 'Digital Marketing Analytics',
        "cycle": 'Marketing & Communications',
        "applicable": True,
        "triggers": ('automated_decision', 'systematic_monitoring'),
        "justification": None,
    },
    {
        "process": 'Customer Loyalty Program Management',
        "cycle": 'Marketing & Communications',
        "applicable": True,
        "triggers": ('large_scale', 'systematic_monitoring'),
        "justification": None,
    },
    {
        "process": 'Event Planning & Sponsorships',
        "cycle": 'Marketing & Communications',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Event Planning & Sponsorships coordinates logistics for a "
            "handful of branded events each year — guest lists, RSVP "
            "contact details and sponsor liaison names — in the same shape "
            "as the CSR community events this register already screens. "
            "Lists are short, static and used only to plan attendance. It "
            "triggers none of the six DPIA screening questions: Large-scale "
            "processing, Special category or highly sensitive data, "
            "Systematic monitoring, New or unproven technology, Automated "
            "decision-making with legal or similarly significant effect, "
            "and Processing involving vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Procurement & Legal (pool 6; 1 applicable, 1 not applicable) -----
    {
        "process": 'Contractor Registration',
        "cycle": 'Procurement & Legal',
        "applicable": True,
        "triggers": ('vulnerable_subjects',),
        "justification": None,
    },
    {
        "process": 'Vendor Registration & KYS',
        "cycle": 'Procurement & Legal',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Vendor Registration & KYS ('know your supplier') records the "
            "company registration details, tax compliance certificate and a "
            "named finance contact for each supplier before a purchase "
            "order is raised. Verification is at company level; the "
            "individual data recorded is limited to a contact person's "
            "name, role and business email, not a government identifier or "
            "a background check. It triggers none of the six DPIA screening "
            "questions: Large-scale processing, Special category or highly "
            "sensitive data, Systematic monitoring, New or unproven "
            "technology, Automated decision-making with legal or similarly "
            "significant effect, and Processing involving vulnerable data "
            "subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Retail Operations (pool 6; 1 applicable, 1 not applicable) -------
    {
        "process": 'Retail Station Incident Reporting',
        "cycle": 'Retail Operations',
        "applicable": True,
        "triggers": ('special_category',),
        "justification": None,
    },
    {
        "process": 'Retail Staff Management',
        "cycle": 'Retail Operations',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Retail Staff Management schedules shifts and rotas for station "
            "attendants using employee name and shift pattern already "
            "recorded under Employee Records Management — it does not "
            "collect any new category of data, and produces no automated "
            "scoring or ranking of staff. It triggers none of the six DPIA "
            "screening questions: Large-scale processing, Special category "
            "or highly sensitive data, Systematic monitoring, New or "
            "unproven technology, Automated decision-making with legal or "
            "similarly significant effect, and Processing involving "
            "vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- QSHE (pool 5; 1 applicable, 1 not applicable) --------------------
    {
        "process": 'QSHE Incident Reporting',
        "cycle": 'QSHE',
        "applicable": True,
        "triggers": ('special_category',),
        "justification": None,
    },
    {
        "process": 'Safety Drill Scheduling',
        "cycle": 'QSHE',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Safety Drill Scheduling plans and logs attendance at routine "
            "fire and spill drills at each depot and station, recording "
            "only staff name and attendance against a scheduled date. No "
            "injury, medical or incident detail is captured here — that is "
            "QSHE Incident Reporting's own, separately screened, process. "
            "It triggers none of the six DPIA screening questions: "
            "Large-scale processing, Special category or highly sensitive "
            "data, Systematic monitoring, New or unproven technology, "
            "Automated decision-making with legal or similarly significant "
            "effect, and Processing involving vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Information Technology (pool 5; 1 app, 1 not applicable) ---------
    {
        "process": 'System Access & Deprovisioning',
        "cycle": 'Information Technology',
        "applicable": True,
        "triggers": ('systematic_monitoring', 'large_scale'),
        "justification": None,
    },
    {
        "process": 'IT Support Ticketing',
        "cycle": 'Information Technology',
        "applicable": False,
        "triggers": (),
        "justification": (
            "IT Support Ticketing logs helpdesk requests — a name, a device "
            "or account reference and a description of the fault — raised "
            "by staff needing IT assistance. Tickets are handled "
            "individually by the support team; nothing is aggregated across "
            "the workforce or used to profile anyone. It triggers none of "
            "the six DPIA screening questions: Large-scale processing, "
            "Special category or highly sensitive data, Systematic "
            "monitoring, New or unproven technology, Automated "
            "decision-making with legal or similarly significant effect, "
            "and Processing involving vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Card Operations (remaining pool 3; 1 app, 1 not applicable) ------
    {
        "process": 'Fuel Card Usage Analytics',
        "cycle": 'Card Operations',
        "applicable": True,
        "triggers": ('automated_decision', 'systematic_monitoring'),
        "justification": None,
    },
    {
        "process": 'Fuel Card Reconciliation',
        "cycle": 'Card Operations',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Fuel Card Reconciliation matches daily card-terminal "
            "settlement batches against the bank's clearing file at an "
            "aggregate, batch level — totals and reference numbers, not the "
            "individual cardholder transactions Fuel Card Issuance's own "
            "screened mapping already covers. It triggers none of the six "
            "DPIA screening questions: Large-scale processing, Special "
            "category or highly sensitive data, Systematic monitoring, New "
            "or unproven technology, Automated decision-making with legal "
            "or similarly significant effect, and Processing involving "
            "vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Engineering & Maintenance (pool 4; 0 app, 1 not applicable) ------
    {
        "process": 'Depot Asset Maintenance',
        "cycle": 'Engineering & Maintenance',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Depot Asset Maintenance schedules servicing for pumps, tanks "
            "and depot machinery, logging only the technician's name "
            "against a work order for accountability. The record is about "
            "the equipment's maintenance history, not about the technician. "
            "It triggers none of the six DPIA screening questions: "
            "Large-scale processing, Special category or highly sensitive "
            "data, Systematic monitoring, New or unproven technology, "
            "Automated decision-making with legal or similarly significant "
            "effect, and Processing involving vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Legal & Compliance (pool 4; 1 applicable, 1 not applicable) ------
    {
        "process": 'Legal Dispute Management',
        "cycle": 'Legal & Compliance',
        "applicable": True,
        "triggers": ('special_category',),
        "justification": None,
    },
    {
        "process": 'Regulatory License Tracking',
        "cycle": 'Legal & Compliance',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Regulatory License Tracking maintains renewal dates and "
            "reference numbers for the company's own operating licenses — "
            "EPRA, NEMA, county trading licenses — held at organisation "
            "level, with a compliance officer's name recorded only as the "
            "internal owner of each renewal task. It triggers none of the "
            "six DPIA screening questions: Large-scale processing, Special "
            "category or highly sensitive data, Systematic monitoring, New "
            "or unproven technology, Automated decision-making with legal "
            "or similarly significant effect, and Processing involving "
            "vulnerable data subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Governance (pool 3; 0 applicable, 1 not applicable) --------------
    {
        "process": 'Meeting Minutes Archival',
        "cycle": 'Governance',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Meeting Minutes Archival files signed minutes of board and "
            "management meetings, naming the directors and managers present "
            "and summarising decisions taken — a small, fixed group acting "
            "in a governance capacity, on record for corporate rather than "
            "personal reasons. It triggers none of the six DPIA screening "
            "questions: Large-scale processing, Special category or highly "
            "sensitive data, Systematic monitoring, New or unproven "
            "technology, Automated decision-making with legal or similarly "
            "significant effect, and Processing involving vulnerable data "
            "subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Audit & Risk (pool 3; 1 applicable, 0 not applicable) ------------
    {
        "process": 'Fraud Risk Assessments',
        "cycle": 'Audit & Risk',
        "applicable": True,
        "triggers": ('automated_decision', 'systematic_monitoring'),
        "justification": None,
    },
    # --- Facilities & Security (pool 2; 1 applicable, 1 not applicable) ---
    {
        "process": 'Visitor Registration & CCTV Monitoring',
        "cycle": 'Facilities & Security',
        "applicable": True,
        "triggers": ('vulnerable_subjects', 'systematic_monitoring'),
        "justification": None,
    },
    {
        "process": 'Workplace Safety Access Control',
        "cycle": 'Facilities & Security',
        "applicable": False,
        "triggers": (),
        "justification": (
            "Workplace Safety Access Control logs staff badge-in and "
            "badge-out events at the head office turnstile, recording "
            "employee name and timestamp against each entry, used only to "
            "produce a same-day safety headcount and never analysed, "
            "aggregated over time, or used to profile individual behaviour "
            "patterns — it does not extend to the CCTV monitoring or "
            "visitor logging that Visitor Registration & CCTV Monitoring "
            "separately screens. It triggers none of the six DPIA screening "
            "questions: Large-scale processing, Special category or highly "
            "sensitive data, Systematic monitoring, New or unproven "
            "technology, Automated decision-making with legal or similarly "
            "significant effect, and Processing involving vulnerable data "
            "subjects."
            + _DEMO_DISCLOSURE
        ),
    },
    # --- Sales & Distribution (pool 2; 1 applicable, 0 not applicable) ----
    {
        "process": 'Distributor & Dealer Onboarding',
        "cycle": 'Sales & Distribution',
        "applicable": True,
        "triggers": ('large_scale',),
        "justification": None,
    },
    # --- Customer onboarding (pool 1; 1 applicable, 0 not applicable) -----
    {
        "process": 'Fuel card application processing',
        "cycle": 'Customer onboarding',
        "applicable": True,
        "triggers": ('large_scale',),
        "justification": None,
    },
)


# --- Data mappings: one per applicable process not already mapped ---------
#
# Fuel Card Issuance (the 24th applicable process) is EXCLUDED here on
# purpose — plan 20 Task 5 already mapped it
# (pri_7b31daa0-0ed3-41d2-8ff0-1ad0afd16b11), and this script's own
# idempotency (seed_mappings()'s _EXISTING_DEMO_MAPPING_SQL) would skip it
# anyway even if it were listed, because that row carries no
# 'privacycare:demo_seed' marker and is never treated as "already done by
# us" — but it is left out of this table entirely so the table's own
# length (23) is legible as "23 NEW mappings", matching the report's own
# arithmetic (24 applicable total = 1 existing + 23 new).
#
# Every `data_subjects` value is a live ctl_data_subjects.fides_key, every
# `data_categories` value a live ctl_data_categories.fides_key, every
# `ground` value one of the eleven USABLE privacycare_processing_ground
# rows (see this module's own docstring), and every `purpose` value a live
# ctl_data_uses.fides_key — test_seed_demo.py checks all four containments
# against the live tables directly, not against a second hand-typed list.
MAPPINGS: tuple[dict, ...] = (
    {
        "process": "Payroll Administration",
        "name": "Payroll processing and statutory remittance",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.financial.bank_account",
            "user.financial.income",
            "user.government_id.tax_pin",
            "user.name",
            "user.contact.email",
        ),
        "ground": "Labour Legislation Compliance",
        "purpose": "essential.service.payment_processing",
        "retention_period": "7 years after employment ends, per Kenya "
        "Revenue Authority and NSSF record-keeping requirements",
        "third_parties": "NSSF, NHIF, KRA, and the company's payroll bank",
    },
    {
        "process": "M-Pesa Integration & Settlement",
        "name": "M-Pesa transaction integration and settlement",
        "data_subjects": ("customer",),
        "data_categories": (
            "user.financial.transaction_history",
            "user.contact.phone_number",
            "user.financial.bank_account",
        ),
        "ground": "Customer Relationship Administration",
        "purpose": "essential.service.payment_processing",
        "retention_period": "5 years, per Central Bank of Kenya "
        "payment-service record-keeping guidance",
        "third_parties": "Safaricom M-Pesa",
    },
    {
        "process": "Insurance Claims Processing",
        "name": "Insurance claims intake and processing",
        "data_subjects": ("beneficiary", "employee"),
        "data_categories": (
            "user.health_and_medical.accident_injuries",
            "user.financial.bank_account",
            "user.name",
            "user.contact.phone_number",
        ),
        "ground": "Obligation of Law (SPI)",
        "purpose": "essential.legal_obligation",
        "retention_period": "7 years after a claim closes, per the "
        "insurer's policy terms",
        "third_parties": "the company's group insurer",
    },
    {
        "process": "OIl Distributor Wallet Transaction Monitoring",
        "name": "Oil distributor wallet transaction monitoring",
        "data_subjects": ("vendor_supplier",),
        "data_categories": (
            "user.financial.transaction_history",
            "user.contact.phone_number",
        ),
        "ground": "Customer Relationship Administration",
        "purpose": "essential.service.payment_processing",
        "retention_period": "5 years, aligned with Central Bank of Kenya "
        "transaction-record guidance",
        "third_parties": None,
    },
    {
        "process": "Driver & Transporter Records",
        "name": "Driver and transporter vetting records",
        "data_subjects": ("independent_contractor",),
        "data_categories": (
            "user.government_id.drivers_license_number",
            "user.name",
            "user.contact.phone_number",
            "user.criminal_history",
        ),
        "ground": "Background Checks and Pre-employment Screening",
        "purpose": "essential.service.security",
        "retention_period": "duration of engagement plus 2 years",
        "third_parties": "a third-party vetting/background-check agency",
    },
    {
        "process": "Fleet Movement Logs",
        "name": "Fleet GPS movement logging",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.location.precise",
            "user.device.device_id",
        ),
        "ground": "Consent by the Data Subject",
        "purpose": "essential.service.security",
        "retention_period": "90 days, then aggregated for reporting",
        "third_parties": None,
    },
    {
        "process": "Medical Records Filing",
        "name": "Employee medical records filing",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.health_and_medical.medical_history",
            "user.health_and_medical.condition_and_treatment",
            "user.name",
        ),
        "ground": "Obligation of Law (SPI)",
        "purpose": "essential.legal_obligation",
        "retention_period": "duration of employment plus 5 years, per "
        "occupational health record-keeping practice",
        "third_parties": None,
    },
    {
        "process": "Recruitment Management",
        "name": "Candidate recruitment and applicant tracking",
        "data_subjects": ("job_applicant",),
        "data_categories": (
            "user.name",
            "user.contact.email",
            "user.contact.phone_number",
            "user.workplace.employment_history",
            "user.education",
        ),
        "ground": "Enrolment of an Applicant",
        "purpose": "employment.recruitment",
        "retention_period": "12 months after a role closes for "
        "unsuccessful candidates",
        "third_parties": None,
    },
    {
        "process": "Employee Records Management",
        "name": "Employee master records management",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.name",
            "user.government_id.national_identification_number",
            "user.contact.address",
            "user.demographic.date_of_birth",
            "user.workplace.employment_history",
        ),
        "ground": "Labour Legislation Compliance",
        "purpose": "essential.legal_obligation",
        "retention_period": "duration of employment plus 7 years",
        "third_parties": "NSSF, NHIF",
    },
    {
        "process": "KYC Documentation Collection (Customer)",
        "name": "Customer KYC documentation collection",
        "data_subjects": ("customer",),
        "data_categories": (
            "user.government_id.national_identification_number",
            "user.government_id.tax_pin",
            "user.name",
            "user.contact.phone_number",
        ),
        "ground": "KYC Requirements",
        "purpose": "essential.legal_obligation",
        "retention_period": "5 years after the customer relationship "
        "ends, per KYC record-keeping practice",
        "third_parties": None,
    },
    {
        "process": "Customer Call Center Operations",
        "name": "Customer call centre operations",
        "data_subjects": ("customer",),
        "data_categories": (
            "user.contact.phone_number",
            "user.behavior.purchase_history",
            "user.name",
        ),
        "ground": "Customer Relationship Administration",
        "purpose": "essential.service.operations.support",
        "retention_period": "12 months for call recordings",
        "third_parties": None,
    },
    {
        "process": "Digital Marketing Analytics",
        "name": "Digital marketing analytics and profiling",
        "data_subjects": ("prospect", "customer"),
        "data_categories": (
            "user.device.cookie_id",
            "user.behavior.browsing_history",
            "user.device.ip_address",
        ),
        "ground": "Marketing",
        "purpose": "analytics.reporting.ad_performance",
        "retention_period": "13 months for analytics identifiers",
        "third_parties": "the company's digital analytics platform "
        "provider",
    },
    {
        "process": "Customer Loyalty Program Management",
        "name": "Customer loyalty programme management",
        "data_subjects": ("customer",),
        "data_categories": (
            "user.behavior.purchase_history",
            "user.contact.phone_number",
            "user.contact.email",
            "user.name",
        ),
        "ground": "Customer Relationship Administration",
        "purpose": "marketing.communications",
        "retention_period": "duration of active membership plus 2 years",
        "third_parties": None,
    },
    {
        "process": "Contractor Registration",
        "name": "Contractor vetting and registration",
        "data_subjects": ("independent_contractor",),
        "data_categories": (
            "user.name",
            "user.government_id.national_identification_number",
            "user.criminal_history",
            "user.contact.phone_number",
        ),
        "ground": "Background Checks and Pre-employment Screening",
        "purpose": "essential.service.security",
        "retention_period": "duration of the contract plus 2 years",
        "third_parties": "a third-party vetting/background-check agency",
    },
    {
        "process": "Retail Station Incident Reporting",
        "name": "Retail station incident reporting",
        "data_subjects": ("employee", "customer"),
        "data_categories": (
            "user.health_and_medical.accident_injuries",
            "user.name",
            "user.contact.phone_number",
        ),
        "ground": "Obligation of Law (SPI)",
        "purpose": "essential.legal_obligation",
        "retention_period": "7 years, per occupational health and safety "
        "record-keeping practice",
        "third_parties": None,
    },
    {
        "process": "QSHE Incident Reporting",
        "name": "QSHE workplace incident reporting",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.health_and_medical.accident_injuries",
            "user.health_and_medical.condition_and_treatment",
            "user.name",
        ),
        "ground": "Obligation of Law (SPI)",
        "purpose": "essential.legal_obligation",
        "retention_period": "7 years, per occupational health and safety "
        "record-keeping practice",
        "third_parties": None,
    },
    {
        "process": "System Access & Deprovisioning",
        "name": "Staff system access provisioning and deprovisioning",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.account.username",
            "user.authorization.credentials",
            "user.unique_id",
        ),
        "ground": "Labour Legislation Compliance",
        "purpose": "essential.service.security",
        "retention_period": "90 days after account deprovisioning",
        "third_parties": None,
    },
    {
        "process": "Fuel Card Usage Analytics",
        "name": "Fuel card usage analytics",
        "data_subjects": ("customer",),
        "data_categories": (
            "user.behavior.purchase_history",
            "user.financial.transaction_history",
        ),
        "ground": "Customer Relationship Administration",
        "purpose": "analytics.reporting",
        "retention_period": "24 months",
        "third_parties": None,
    },
    {
        "process": "Legal Dispute Management",
        "name": "Legal dispute case management",
        "data_subjects": ("employee",),
        "data_categories": (
            "user.content.private.correspondence",
            "user.health_and_medical.condition_and_treatment",
            "user.name",
        ),
        "ground": "Obligation of Law (SPI)",
        "purpose": "essential.legal_obligation",
        "retention_period": "7 years after a matter closes, per "
        "limitation-period practice",
        "third_parties": "the company's external legal counsel",
    },
    {
        "process": "Fraud Risk Assessments",
        "name": "Fraud risk assessment and monitoring",
        "data_subjects": ("customer",),
        "data_categories": (
            "user.financial.transaction_history",
            "user.behavior.purchase_history",
            "user.unique_id",
        ),
        "ground": "Obligation of Law (SPI)",
        "purpose": "essential.fraud_detection",
        "retention_period": "5 years, per anti-fraud record-keeping "
        "practice",
        "third_parties": None,
    },
    {
        "process": "Visitor Registration & CCTV Monitoring",
        "name": "Visitor registration and CCTV monitoring",
        "data_subjects": ("visitor",),
        "data_categories": (
            "user.name",
            "user.contact.phone_number",
            "user.content.self_image",
        ),
        "ground": "Consent by the Data Subject",
        "purpose": "essential.service.security",
        "retention_period": "30 days for CCTV footage; 12 months for the "
        "visitor log",
        "third_parties": None,
    },
    {
        "process": "Distributor & Dealer Onboarding",
        "name": "Distributor and dealer onboarding KYC",
        "data_subjects": ("vendor_supplier",),
        "data_categories": (
            "user.name",
            "user.government_id.national_identification_number",
            "user.contact.phone_number",
            "user.financial.bank_account",
        ),
        "ground": "KYC Requirements",
        "purpose": "essential.legal_obligation",
        "retention_period": "duration of the distributor agreement plus "
        "5 years",
        "third_parties": None,
    },
    {
        "process": "Fuel card application processing",
        "name": "Fuel card applicant KYC processing",
        "data_subjects": ("applicant",),
        "data_categories": (
            "user.government_id.national_identification_number",
            "user.government_id.tax_pin",
            "user.name",
            "user.contact.phone_number",
            "user.financial.bank_account",
        ),
        "ground": "Enrolment of an Applicant",
        "purpose": "essential.legal_obligation",
        "retention_period": "5 years after the application decision, per "
        "KYC record-keeping practice",
        "third_parties": None,
    },
)


# --- DSR requests: one per Kenyan right, spread across the alerting states -
#
# Each entry: process — one of Josephine's REAL business processes (or None,
# left deliberately unlinked — see this module's own docstring, "D-SEED-8"),
# right — one of dsr.timelines.KENYAN_RIGHTS, name/contact — a realistic
# Kenyan requester identity (never written verbatim: _dsr_subject_identifier
# below appends the demo marker), role_note — documentation only, never
# written to the database, days_ago — how many days before this script runs
# the request was "received", which this module's own docstring works out
# against dsr/alerts.py's REAL warn_threshold for each right's clock.
DSR_REQUESTS: tuple[dict, ...] = (
    {
        "process": "Fuel card application processing",
        "right": "access",
        "name": "Grace Wanjiru",
        "contact": "+254 712 345 678",
        "role_note": "fuel card customer requesting a copy of the personal "
        "data collected on her card application",
        "days_ago": 1,  # comfortably inside a 7-day clock (6 days left)
    },
    {
        "process": "Customer Loyalty Program Management",
        "right": "access",
        "name": "Brian Otieno",
        "contact": "+254 733 987 654",
        "role_note": "loyalty programme member requesting his purchase "
        "history held under the programme",
        "days_ago": 5,  # close to breach on a 7-day clock (2 days left)
    },
    {
        "process": "Employee Records Management",
        "right": "rectification",
        "name": "Mercy Achieng",
        "contact": "mercy.achieng.hr@example.invalid",
        "role_note": "payroll employee correcting a transposed digit in "
        "her KRA PIN as recorded in her HR file",
        "days_ago": 10,  # close to breach on a 14-day clock (4 days left)
    },
    {
        "process": None,  # deliberately unlinked — see this module's docstring
        "right": "erasure",
        "name": "Peter Kamau",
        "contact": "+254 701 222 333",
        "role_note": "former call-centre caller asking for his recorded "
        "call history to be deleted now that the matter is closed",
        "days_ago": 17,  # OVERDUE on a 14-day clock (3 days past deadline)
    },
    {
        "process": "Driver & Transporter Records",
        "right": "restriction",
        "name": "Samuel Kiptoo",
        "contact": "+254 720 456 789",
        "role_note": "contracted driver asking that his GPS movement logs "
        "not be used while a grievance about his last route is open",
        "days_ago": 9,  # AT the warn threshold on a 14-day clock (5 left)
    },
    {
        "process": "M-Pesa Integration & Settlement",
        "right": "portability",
        "name": "Faith Nyambura",
        "contact": "+254 798 111 222",
        "role_note": "M-Pesa wallet customer requesting her transaction "
        "history in a portable format to switch service providers",
        "days_ago": 5,  # comfortably inside a 30-day clock (25 days left)
    },
    {
        "process": "Digital Marketing Analytics",
        "right": "objection",
        "name": "Daniel Mwangi",
        "contact": "+254 711 654 321",
        "role_note": "customer objecting to being profiled for targeted "
        "marketing based on his fuel purchase history",
        "days_ago": 0,  # unclocked — must show NO deadline regardless of age
    },
)


# --- Consent: one fresh, one stale, against seed_consent_demo.py's own notice
#
# "fresh"/"stale" here means version lag against the LIVE notice version,
# never elapsed time — see this module's own docstring, "D-SEED-9", for why.
# `version` selects which of seed_consent_demo.py's own two history ids
# (v1 or v2, both already live in the database before this script ever
# runs) the new preference row is recorded against.
CONSENT_ADDITIONS: tuple[dict, ...] = (
    {
        "label": "fresh",
        "email": "alice.wambui.consent@example.invalid",
        "version": "v2",  # the LIVE version — not stale, provably
        "days_old": 2,  # narrative only; the detector never reads this
    },
    {
        "label": "stale",
        "email": "kevin.mutiso.consent@example.invalid",
        "version": "v1",  # missing marketing.advertising.third_party
        "days_old": 400,  # narrative only; the detector never reads this
    },
)


def _database_url() -> str:
    # Copied from scripts/privacycare/seed_dsr.py's _database_url()
    # verbatim (same precedence, same env vars, same defaults) rather than
    # imported — see load_taxonomy.py's module docstring for why importing
    # env.py isn't safe here.
    return os.environ.get(
        "PRIVACYCARE_DATABASE_URL",
        "postgresql://{u}:{p}@{h}:{port}/{db}".format(
            u=os.environ.get("FIDES__DATABASE__USER", "postgres"),
            p=os.environ.get("FIDES__DATABASE__PASSWORD", "fides"),
            h=os.environ.get("FIDES__DATABASE__SERVER", "127.0.0.1"),
            port=os.environ.get("FIDES__DATABASE__PORT", "5442"),
            db=os.environ.get("FIDES__DATABASE__DB", "fides"),
        ),
    )


def _target_description(database_url: str) -> str:
    """Render `database_url` as `user@host:port/db` for the pre-write
    target line. NEVER includes the password, and never prints the raw
    URL — a URL with embedded credentials is exactly what a consultant
    should not paste into a terminal transcript or a ticket.
    """
    parsed = make_url(database_url)
    user = parsed.username or ""
    location = parsed.host or ""
    if parsed.port:
        location = f"{location}:{parsed.port}"
    database = parsed.database or ""
    return f"{user}@{location}/{database}"


_FIND_BUSINESS_PROCESS_SQL = sqlalchemy.text(
    "SELECT id FROM privacycare_business_process "
    "WHERE name = :name AND deleted_at IS NULL"
)


def _find_business_process_id(db: Session, name: str) -> str:
    bp_id = db.execute(_FIND_BUSINESS_PROCESS_SQL, {"name": name}).scalar()
    if bp_id is None:
        raise ValueError(
            f"no such business process: {name!r} — the register may have "
            "changed since this seed's SCREENING_DECISIONS/MAPPINGS tables "
            "were written"
        )
    return bp_id


# --- Screening decisions -----------------------------------------------


def seed_screening_decisions(db: Session) -> dict:
    """Writes every SCREENING_DECISIONS entry whose business process has no
    decision yet, through gate.record_decision() — never a raw INSERT (see
    this module's own docstring, "GOES THROUGH THE PRODUCT'S OWN CODE
    PATH"). Idempotent: a process that already has ANY decision (ours from
    an earlier run, or otherwise) is skipped, because
    privacycare_screening_decision is append-only by convention and this
    script must never append a second, contradictory decision under a
    caller who reruns it — see this module's own docstring, "RECORD_
    DECISION IS APPEND-ONLY, WHICH IS WHAT MAKES SCREENING IDEMPOTENT."
    Never commits — the caller's session boundary decides.
    """
    written = 0
    skipped = 0
    applicable_written = 0
    not_applicable_written = 0
    for entry in SCREENING_DECISIONS:
        bp_id = _find_business_process_id(db, entry["process"])
        if gate.current_verdict(db, bp_id) is not None:
            skipped += 1
            continue
        gate.record_decision(
            db,
            business_process_id=bp_id,
            triggered_keys=list(entry["triggers"]),
            justification=entry["justification"],
            decided_by=DECIDED_BY_MARKER,
        )
        written += 1
        if entry["applicable"]:
            applicable_written += 1
        else:
            not_applicable_written += 1
    return {
        "decisions_written": written,
        "decisions_skipped": skipped,
        "decisions_applicable_written": applicable_written,
        "decisions_not_applicable_written": not_applicable_written,
    }


# --- Data mappings -----------------------------------------------------

# This script's OWN idempotency for mappings — deliberately NOT
# mapping.get_mapping()/mapping._EXISTING_ROUTE_ACTIVITY_SQL, which key on
# save_mapping()'s own 'privacycare:mapping_route' marker and would also
# match the 2 pre-existing demonstration mappings this script must never
# touch. Keyed on DEMO_FEATURE_MARKER alone, so a second run of this script
# recognises exactly (and only) the rows THIS script created.
_EXISTING_DEMO_MAPPING_SQL = sqlalchemy.text(
    "SELECT pd.id "
    "FROM privacycare_process_declaration link "
    "JOIN privacydeclaration pd ON pd.id = link.privacy_declaration_id "
    "WHERE link.business_process_id = :business_process_id "
    "AND :marker = ANY(pd.features) "
    "LIMIT 1"
)

# Appends DEMO_FEATURE_MARKER to whatever save_mapping() already wrote —
# see this module's own docstring, "WHY THE MARKER IS APPENDED, NOT
# SUBSTITUTED". The `NOT (:marker = ANY(features))` guard makes a repeated
# call to this statement a no-op rather than a duplicate array entry, the
# same idiom _APPEND_SYSTEM_MARKER_SQL below uses.
_APPEND_DECLARATION_MARKER_SQL = sqlalchemy.text(
    "UPDATE privacydeclaration SET features = array_append(features, :marker) "
    "WHERE id = :id AND NOT (:marker = ANY(features))"
)

_APPEND_SYSTEM_MARKER_SQL = sqlalchemy.text(
    "UPDATE ctl_systems SET tags = array_append(tags, :marker) "
    "WHERE id = :id AND NOT (:marker = ANY(tags))"
)


def seed_mappings(db: Session) -> dict:
    """Writes every MAPPINGS entry whose business process has no
    demo-marked mapping yet, through mapping.save_mapping() — never a raw
    INSERT into privacydeclaration (see this module's own docstring, "GOES
    THROUGH THE PRODUCT'S OWN CODE PATH"). After save_mapping() creates the
    activity, this function appends DEMO_FEATURE_MARKER onto
    privacydeclaration.features, and — ONLY when this call provisioned a
    brand-new ctl_systems row rather than reusing an existing one (checked
    BEFORE calling save_mapping(), via mapping.existing_system_id_for_
    process()) — onto that system's ctl_systems.tags too. A REUSED system
    (a real customer one, such as Fuel card application processing's own
    fuel_card_crm, or one an earlier iteration of this same process already
    tagged) is never retagged.

    Idempotent: a process whose _EXISTING_DEMO_MAPPING_SQL check already
    finds a demo-marked activity is skipped entirely — save_mapping() is
    not called a second time for it. Never commits — the caller's session
    boundary decides.
    """
    written = 0
    skipped = 0
    systems_marked = 0
    for entry in MAPPINGS:
        bp_id = _find_business_process_id(db, entry["process"])

        already = db.execute(
            _EXISTING_DEMO_MAPPING_SQL,
            {"business_process_id": bp_id, "marker": DEMO_FEATURE_MARKER},
        ).scalar()
        if already is not None:
            skipped += 1
            continue

        pre_existing_system_id = mapping_module.existing_system_id_for_process(
            db, bp_id
        )

        result = mapping_module.save_mapping(
            db,
            business_process_id=bp_id,
            name=entry["name"],
            data_categories=list(entry["data_categories"]),
            recorded_by=DECIDED_BY_MARKER,
            data_subjects=list(entry["data_subjects"]),
            ground=entry["ground"],
            purpose=entry["purpose"],
            retention_period=entry.get("retention_period"),
            third_parties=entry.get("third_parties"),
        )

        db.execute(
            _APPEND_DECLARATION_MARKER_SQL,
            {"id": result.privacy_declaration_id, "marker": DEMO_FEATURE_MARKER},
        )

        if pre_existing_system_id is None:
            # This call provisioned a brand-new system for this process —
            # ours to mark. See this function's own docstring for why a
            # reused system is never touched.
            db.execute(
                _APPEND_SYSTEM_MARKER_SQL,
                {"id": result.system_id, "marker": DEMO_FEATURE_MARKER},
            )
            systems_marked += 1

        written += 1
    return {
        "mappings_written": written,
        "mappings_skipped": skipped,
        "systems_marked": systems_marked,
    }


# --- DSR requests (D-SEED-8) ------------------------------------------


def _dsr_subject_identifier(name: str, contact: str) -> str:
    """Renders a DSR_REQUESTS entry's identity as the one string that goes
    into `subject_identifier` — a realistic Kenyan name and contact detail,
    with DEMO_FEATURE_MARKER appended as a bracketed, self-disclosing
    suffix. See this module's own docstring, "D-SEED-8", for why
    subject_identifier (not owner_email or decided_by) carries the marker
    on this table. Deterministic per (name, contact) pair — the same
    string this script's own idempotency check
    (_EXISTING_DEMO_DSR_REQUEST_SQL) keys an exact match on."""
    return (
        f"{name} — {contact} "
        f"[{DEMO_FEATURE_MARKER} — synthetic demo requester, not a real "
        "data subject]"
    )


_EXISTING_DEMO_DSR_REQUEST_SQL = sqlalchemy.text(
    "SELECT id FROM privacycare_dsr_request WHERE subject_identifier = :subject_identifier"
)


def seed_dsr_requests(db: Session) -> dict:
    """Writes every DSR_REQUESTS entry whose exact subject_identifier isn't
    already on the register, through dsr.register.record_request() — never
    a raw INSERT into privacycare_dsr_request (mirrors this module's own
    "GOES THROUGH THE PRODUCT'S OWN CODE PATH" rule for screening/mapping).
    Calls dsr.timelines.seed_timelines(db) first — idempotent, reused
    directly from the same module seed_dsr.py itself calls, never
    duplicated — because record_request() raises if
    privacycare_dsr_timeline has no row for a right at all (an unseeded
    database, distinct from objection's real, seeded NULL). Never
    delegates (see this module's own docstring for why). Never commits —
    the caller's session boundary decides.
    """
    seed_timelines(db)

    now = datetime.now(timezone.utc)
    written = 0
    skipped = 0
    for entry in DSR_REQUESTS:
        subject_identifier = _dsr_subject_identifier(entry["name"], entry["contact"])
        existing = db.execute(
            _EXISTING_DEMO_DSR_REQUEST_SQL,
            {"subject_identifier": subject_identifier},
        ).scalar()
        if existing is not None:
            skipped += 1
            continue

        business_process_id = (
            _find_business_process_id(db, entry["process"])
            if entry["process"]
            else None
        )
        received_at = now - timedelta(days=entry["days_ago"])
        dsr_register.record_request(
            db,
            right=entry["right"],
            subject_identifier=subject_identifier,
            business_process_id=business_process_id,
            received_at=received_at,
        )
        written += 1

    return {"dsr_requests_written": written, "dsr_requests_skipped": skipped}


# --- Consent (D-SEED-9) -------------------------------------------------

_SEED_CONSENT_DEMO_PATH = (
    pathlib.Path(__file__).resolve().parent / "seed_consent_demo.py"
)
_seed_consent_demo_module_cache = None


def _seed_consent_demo_module():
    """Loads scripts/privacycare/seed_consent_demo.py the same way
    tests/privacycare/test_seed_consent_demo.py and test_seed_demo.py
    already load a script from this package — scripts/privacycare carries
    no __init__.py, so `import seed_consent_demo` is not available, and
    `importlib.util.spec_from_file_location` is the established way in.
    Cached at module scope: seed_demo()/seed_consent() may each be called
    more than once in one process (this script's own idempotency tests do
    exactly that), and re-executing the module body every call would be
    wasted work — seed_consent_demo.py has no import-time side effect
    beyond def/constant statements, so caching changes nothing but cost.
    """
    global _seed_consent_demo_module_cache
    if _seed_consent_demo_module_cache is None:
        spec = importlib.util.spec_from_file_location(
            "privacycare_seed_consent_demo", _SEED_CONSENT_DEMO_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _seed_consent_demo_module_cache = module
    return _seed_consent_demo_module_cache


_EXISTING_DEMO_PREFERENCE_SQL = sqlalchemy.text(
    "SELECT id FROM privacypreferencehistory "
    "WHERE url_recorded = :marker AND privacy_notice_history_id = :history_id "
    "LIMIT 1"
)


def seed_consent(db: Session) -> dict:
    """Ensures seed_consent_demo.py's own rule/notice/versions/preference
    exist (idempotent — reused via its own public seed_consent_demo(db),
    never duplicated), then adds this script's own two NEW preference rows
    (one against v2, the live version — fresh; one against v1 — stale) —
    both through PrivacyPreferenceHistory.create(), never raw SQL, for the
    same encryption reason seed_consent_demo.py's own module docstring
    gives. Idempotent on an exact (url_recorded, privacy_notice_history_id)
    match. Never commits itself — but see this module's own docstring,
    "SCREENING AND MAPPING ARE RAW SQL; DSR AND CONSENT ARE NOT": the ORM
    create() calls inside this function DO call persist_obj's own
    unconditional db.commit(), which main() below absorbs into a flush for
    the duration of the whole seed/remove call, the same guard seed_dsr.py
    and seed_consent_demo.py's own main() already rely on.
    """
    demo_consent = _seed_consent_demo_module()
    base = demo_consent.seed_consent_demo(db)
    history_ids = {"v1": base["v1_id"], "v2": base["v2_id"]}

    now = datetime.now(timezone.utc)
    written = 0
    skipped = 0
    for entry in CONSENT_ADDITIONS:
        history_id = history_ids[entry["version"]]
        existing = db.execute(
            _EXISTING_DEMO_PREFERENCE_SQL,
            {"marker": DEMO_FEATURE_MARKER, "history_id": history_id},
        ).scalar()
        if existing is not None:
            skipped += 1
            continue

        PrivacyPreferenceHistory.create(
            db,
            data={
                "preference": "opt_in",
                "privacy_notice_history_id": history_id,
                "email": entry["email"],
                "url_recorded": DEMO_FEATURE_MARKER,
                "received_at": now - timedelta(days=entry["days_old"]),
            },
            check_name=False,
        )
        written += 1

    return {
        "consent_preferences_written": written,
        "consent_preferences_skipped": skipped,
    }


def seed_demo(db: Session) -> dict:
    """Screening decisions, then data mappings, then DSR requests, then
    consent — mappings depend on nothing screening writes and DSR/consent
    depend on nothing either of the first two write, but the design doc's
    own decision order (D-SEED-4 before D-SEED-5's mappings, D-SEED-8
    before D-SEED-9) is followed here too, so a partial run's own log
    reads in the same order a consultant would work through it by hand.
    Never commits — the caller's session boundary decides.
    """
    summary = seed_screening_decisions(db)
    summary.update(seed_mappings(db))
    summary.update(seed_dsr_requests(db))
    summary.update(seed_consent(db))
    return summary


# --- Removal -------------------------------------------------------------

_SELECT_DEMO_DECLARATION_IDS_SQL = sqlalchemy.text(
    "SELECT id FROM privacydeclaration WHERE :marker = ANY(features)"
)
_SELECT_DEMO_SYSTEM_IDS_SQL = sqlalchemy.text(
    "SELECT id FROM ctl_systems WHERE :marker = ANY(tags)"
)
# privacycare_declaration_ground carries NO ForeignKey to privacydeclaration
# (mapping.py's own docstring: "privacycare_declaration_ground has no FK to
# privacydeclaration") — deleting the declaration would leave this row
# stranded, not cascade it, so it is deleted explicitly, first.
_DELETE_DECLARATION_GROUND_SQL = sqlalchemy.text(
    "DELETE FROM privacycare_declaration_ground "
    "WHERE privacy_declaration_id = ANY(:ids)"
)
# privacycare_process_declaration.privacy_declaration_id carries no FK
# either (same reason, same docstring) — deleted explicitly, before the
# declaration itself.
_DELETE_PROCESS_DECLARATION_LINK_SQL = sqlalchemy.text(
    "DELETE FROM privacycare_process_declaration "
    "WHERE privacy_declaration_id = ANY(:ids)"
)
_DELETE_DECLARATION_SQL = sqlalchemy.text(
    "DELETE FROM privacydeclaration WHERE id = ANY(:ids)"
)
# privacydeclaration.system_id -> ctl_systems.id is NO ACTION (measured
# live against information_schema.referential_constraints), so any
# declaration referencing a system must be deleted BEFORE that system —
# the declaration DELETE above always runs first in remove_demo() for
# exactly this reason.
_DELETE_SYSTEM_SQL = sqlalchemy.text("DELETE FROM ctl_systems WHERE id = ANY(:ids)")
_DELETE_SCREENING_DECISIONS_SQL = sqlalchemy.text(
    "DELETE FROM privacycare_screening_decision WHERE decided_by = :decided_by"
)

# DSR requests (D-SEED-8): scoped by a LIKE match on subject_identifier —
# the one exact-equality trick the other tables' array columns use
# (`:marker = ANY(features)`) doesn't apply to a scalar free-text column
# whose content also varies per row (name and contact differ every time).
# The pattern still anchors on the FULL literal marker string
# ("privacycare:demo_seed", never a fragment of it), so this is a marker
# match, not a guess at ids or a date range. privacycare_dsr_alert rows
# cascade automatically (migration 3a48cbf9edc3_dsr_alert.py:
# ondelete='CASCADE' on dsr_request_id) — no explicit DELETE needed here.
_SELECT_DEMO_DSR_REQUEST_IDS_SQL = sqlalchemy.text(
    "SELECT id FROM privacycare_dsr_request WHERE subject_identifier LIKE :pattern"
)
_DELETE_DSR_REQUEST_SQL = sqlalchemy.text(
    "DELETE FROM privacycare_dsr_request WHERE id = ANY(:ids)"
)

# Consent (D-SEED-9): scoped by an EXACT match on url_recorded — the one
# unencrypted free-text column this script writes DEMO_FEATURE_MARKER into
# (see this module's own docstring, "THE MARKER, ON THE ONE UNENCRYPTED
# FREE-TEXT COLUMN THIS TABLE HAS"). seed_consent_demo.py's own
# pre-existing preference row never sets url_recorded (NULL), so it is
# never selected here.
_SELECT_DEMO_PREFERENCE_IDS_SQL = sqlalchemy.text(
    "SELECT id FROM privacypreferencehistory WHERE url_recorded = :marker"
)
_DELETE_PREFERENCE_SQL = sqlalchemy.text(
    "DELETE FROM privacypreferencehistory WHERE id = ANY(:ids)"
)


def remove_demo(db: Session) -> dict:
    """Deletes every row this script (or an earlier run of it) marked —
    and ONLY those rows. Every SELECT and DELETE below is scoped by
    DEMO_FEATURE_MARKER or DECIDED_BY_MARKER; there is no id list, no date
    range, no "everything created today" — the brief's own words: "a
    removal that guesses will one day delete something of hers." The 2
    pre-existing demonstration decisions, 4 processing activities, 5
    links, 3 systems and 3 risks the demo-rows inventory documents, PLUS
    seed_consent_demo.py's own pre-existing preference row (and its notice,
    versions and rule row), carry NEITHER marker and are never selected by
    any query here — see this module's own docstring, "REMOVAL NEVER
    TOUCHES SEED_CONSENT_DEMO.PY'S OWN ROW...".

    Deletion order respects the FK/no-FK shape measured against the live
    schema (see each DELETE statement's own comment): declaration_ground
    (no FK) and process_declaration links (no FK) before the declaration
    itself; the declaration (NO ACTION FK to ctl_systems) before the
    system. Screening decisions, DSR requests and consent preferences have
    no dependency on any of the above (or on each other) and are removed
    independently. Never commits — the caller's session boundary decides.
    """
    declaration_ids = [
        row[0]
        for row in db.execute(
            _SELECT_DEMO_DECLARATION_IDS_SQL, {"marker": DEMO_FEATURE_MARKER}
        ).all()
    ]
    system_ids = [
        row[0]
        for row in db.execute(
            _SELECT_DEMO_SYSTEM_IDS_SQL, {"marker": DEMO_FEATURE_MARKER}
        ).all()
    ]

    grounds_removed = 0
    links_removed = 0
    declarations_removed = 0
    systems_removed = 0

    if declaration_ids:
        grounds_removed = db.execute(
            _DELETE_DECLARATION_GROUND_SQL, {"ids": declaration_ids}
        ).rowcount
        links_removed = db.execute(
            _DELETE_PROCESS_DECLARATION_LINK_SQL, {"ids": declaration_ids}
        ).rowcount
        declarations_removed = db.execute(
            _DELETE_DECLARATION_SQL, {"ids": declaration_ids}
        ).rowcount

    if system_ids:
        systems_removed = db.execute(
            _DELETE_SYSTEM_SQL, {"ids": system_ids}
        ).rowcount

    decisions_removed = db.execute(
        _DELETE_SCREENING_DECISIONS_SQL, {"decided_by": DECIDED_BY_MARKER}
    ).rowcount

    dsr_request_ids = [
        row[0]
        for row in db.execute(
            _SELECT_DEMO_DSR_REQUEST_IDS_SQL,
            {"pattern": f"%{DEMO_FEATURE_MARKER}%"},
        ).all()
    ]
    dsr_requests_removed = 0
    if dsr_request_ids:
        dsr_requests_removed = db.execute(
            _DELETE_DSR_REQUEST_SQL, {"ids": dsr_request_ids}
        ).rowcount

    preference_ids = [
        row[0]
        for row in db.execute(
            _SELECT_DEMO_PREFERENCE_IDS_SQL, {"marker": DEMO_FEATURE_MARKER}
        ).all()
    ]
    preferences_removed = 0
    if preference_ids:
        preferences_removed = db.execute(
            _DELETE_PREFERENCE_SQL, {"ids": preference_ids}
        ).rowcount

    return {
        "decisions_removed": decisions_removed,
        "declarations_removed": declarations_removed,
        "links_removed": links_removed,
        "grounds_removed": grounds_removed,
        "systems_removed": systems_removed,
        "dsr_requests_removed": dsr_requests_removed,
        "preferences_removed": preferences_removed,
    }


# --- Counts, for the pre/post report ---------------------------------------

_COUNTS_SQL = {
    "screening_decisions_total": (
        "SELECT count(*) FROM privacycare_screening_decision"
    ),
    "screening_decisions_applicable": (
        "SELECT count(*) FROM privacycare_screening_decision "
        "WHERE dpia_required = true"
    ),
    "screening_decisions_not_applicable": (
        "SELECT count(*) FROM privacycare_screening_decision "
        "WHERE dpia_required = false"
    ),
    "privacydeclaration_total": "SELECT count(*) FROM privacydeclaration",
    "ctl_systems_total": "SELECT count(*) FROM ctl_systems",
    "process_declaration_links_total": (
        "SELECT count(*) FROM privacycare_process_declaration"
    ),
    "declaration_ground_total": (
        "SELECT count(*) FROM privacycare_declaration_ground"
    ),
    "dpia_risk_total": "SELECT count(*) FROM privacycare_dpia_risk",
    "privacy_assessment_total": "SELECT count(*) FROM privacy_assessment",
    "dsr_requests_total": "SELECT count(*) FROM privacycare_dsr_request",
    "dsr_requests_open": (
        "SELECT count(*) FROM privacycare_dsr_request WHERE status = 'open'"
    ),
    "privacypreferencehistory_total": (
        "SELECT count(*) FROM privacypreferencehistory"
    ),
    # Context only, never touched by this script — proves seed_consent_demo.py's
    # own rule/notice rows survive this task's seed/remove cycle untouched.
    "privacycare_consent_rule_total": (
        "SELECT count(*) FROM privacycare_consent_rule"
    ),
    "privacynotice_total": "SELECT count(*) FROM privacynotice",
}


def gather_counts(db: Session) -> dict:
    """Every count this task's report cites, in one place, so the report's
    own before/after/twice/removed table is built from the SAME queries a
    reviewer could run by hand. PUBLIC (no leading underscore) so both
    main() and this script's own tests can call it without going through
    a subprocess.
    """
    counts = {
        key: db.execute(sqlalchemy.text(sql)).scalar()
        for key, sql in _COUNTS_SQL.items()
    }
    counts["demo_marked_declarations"] = len(
        db.execute(
            _SELECT_DEMO_DECLARATION_IDS_SQL, {"marker": DEMO_FEATURE_MARKER}
        ).all()
    )
    counts["demo_marked_systems"] = len(
        db.execute(
            _SELECT_DEMO_SYSTEM_IDS_SQL, {"marker": DEMO_FEATURE_MARKER}
        ).all()
    )
    counts["demo_marked_decisions"] = db.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM privacycare_screening_decision "
            "WHERE decided_by = :decided_by"
        ),
        {"decided_by": DECIDED_BY_MARKER},
    ).scalar()
    counts["dsr_requests_demo_marked"] = len(
        db.execute(
            _SELECT_DEMO_DSR_REQUEST_IDS_SQL,
            {"pattern": f"%{DEMO_FEATURE_MARKER}%"},
        ).all()
    )
    counts["privacypreferencehistory_demo_marked"] = len(
        db.execute(
            _SELECT_DEMO_PREFERENCE_IDS_SQL, {"marker": DEMO_FEATURE_MARKER}
        ).all()
    )
    return counts


def _print_counts(counts: dict) -> None:
    print("counts:")
    for key in (
        "screening_decisions_total",
        "screening_decisions_applicable",
        "screening_decisions_not_applicable",
        "privacydeclaration_total",
        "ctl_systems_total",
        "process_declaration_links_total",
        "declaration_ground_total",
        "dpia_risk_total",
        "privacy_assessment_total",
        "demo_marked_declarations",
        "demo_marked_systems",
        "demo_marked_decisions",
        "dsr_requests_total",
        "dsr_requests_open",
        "dsr_requests_demo_marked",
        "privacypreferencehistory_total",
        "privacypreferencehistory_demo_marked",
        "privacycare_consent_rule_total",
        "privacynotice_total",
    ):
        print(f"  {key}: {counts[key]}")


def _print_seed_summary(summary: dict) -> None:
    print(
        "screening decisions written: "
        f"{summary['decisions_written']} "
        f"(applicable: {summary['decisions_applicable_written']}, "
        f"not applicable: {summary['decisions_not_applicable_written']}); "
        f"skipped (already decided): {summary['decisions_skipped']}"
    )
    print(
        "data mappings written: "
        f"{summary['mappings_written']}; "
        f"skipped (already mapped by this seed): {summary['mappings_skipped']}; "
        f"systems provisioned and marked: {summary['systems_marked']}"
    )
    print(
        "DSR requests written: "
        f"{summary['dsr_requests_written']}; "
        f"skipped (already on the register): {summary['dsr_requests_skipped']}"
    )
    print(
        "consent preferences written: "
        f"{summary['consent_preferences_written']}; "
        f"skipped (already recorded): {summary['consent_preferences_skipped']}"
    )


def _print_removal_summary(summary: dict) -> None:
    print(
        "removed — screening decisions: "
        f"{summary['decisions_removed']}, "
        f"data mappings: {summary['declarations_removed']}, "
        f"process-declaration links: {summary['links_removed']}, "
        f"declaration grounds: {summary['grounds_removed']}, "
        f"systems: {summary['systems_removed']}, "
        f"DSR requests: {summary['dsr_requests_removed']}, "
        f"consent preferences: {summary['preferences_removed']}"
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the PrivacyCare demo data: screening decisions "
        "on 38 business processes (23 applicable, 15 not applicable, "
        "spread across business cycles), a data mapping for each of the "
        "24 applicable processes lacking one, 7 DSR requests spread across "
        "the Kenyan clocks' alerting states (comfortably inside, close to "
        "breach, overdue, and one unclocked objection), and 2 consent "
        "preferences (one fresh, one stale) against seed_consent_demo.py's "
        "own notice. Every row carries the 'privacycare:demo_seed' marker "
        "(privacydeclaration.features / ctl_systems.tags / "
        "privacycare_screening_decision.decided_by / "
        "privacycare_dsr_request.subject_identifier / "
        "privacypreferencehistory.url_recorded) and is fully removable "
        "with --remove."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually commit the transaction. Without this, the run is a "
        "dry run (rolled back, nothing written).",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove every row carrying the 'privacycare:demo_seed' "
        "marker instead of seeding. Combine with --commit to actually "
        "delete; without it, reports what would be removed and rolls "
        "back, same as the default seeding mode.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    database_url = _database_url()
    # Name the target BEFORE opening a session or writing anything, so a
    # consultant pointing this at the wrong database finds out immediately.
    # Never the password, never the raw URL.
    print(f"target: {_target_description(database_url)}")

    engine = sqlalchemy.create_engine(database_url)
    # Not `with Session(engine) as db:` — the sqlmypy plugin (still 1.x-era,
    # per pyproject.toml's [tool.mypy] plugins) doesn't see Session's
    # context-manager protocol and flags __enter__/__exit__ as missing.
    # try/finally gets the same close-on-exit guarantee without tripping it.
    db = Session(engine)
    try:
        # See this module's own docstring, "SCREENING AND MAPPING ARE RAW
        # SQL; DSR AND CONSENT ARE NOT": seed_consent()'s ORM writes
        # (PrivacyPreferenceHistory.create, both its own and
        # seed_consent_demo.py's) go through Fides' base_class.persist_obj,
        # which calls db.commit() UNCONDITIONALLY. Absorbing that into a
        # flush for the duration of the seed/remove call is the exact
        # pattern seed_dsr.py's and seed_consent_demo.py's own main()
        # already use — restored immediately after (success or failure
        # alike), so the REAL db.commit()/db.rollback() below (driven by
        # args.commit) is what actually decides whether anything survives.
        # Harmless for remove_demo() and the screening/mapping/DSR paths,
        # which are all raw SQL with no persist_obj call to trip.
        real_commit = db.commit
        db.commit = db.flush  # type: ignore[method-assign]
        try:
            if args.remove:
                print("mode: REMOVE")
                summary = remove_demo(db)
            else:
                print("mode: SEED")
                summary = seed_demo(db)
        except ValueError as exc:
            db.commit = real_commit  # type: ignore[method-assign]
            db.rollback()
            print(str(exc), file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            # A connection failure or a database error (e.g. an
            # IntegrityError) raised while seeding/removing. NEVER print
            # str(exc) or the raw database_url: DBAPI driver error text can
            # itself embed the DSN, credentials included, on some drivers —
            # the same leak route every sibling PrivacyCare seed script's
            # own handler guards against. Only the exception's class name
            # and the already-safe target line's contents (host/port/db,
            # never the password) go to stderr.
            db.commit = real_commit  # type: ignore[method-assign]
            try:
                db.rollback()
            except SQLAlchemyError:
                pass
            print(
                f"error: database operation failed against "
                f"{_target_description(database_url)} "
                f"({type(exc).__name__}) — nothing written",
                file=sys.stderr,
            )
            return 1
        db.commit = real_commit  # type: ignore[method-assign]

        if args.remove:
            _print_removal_summary(summary)
        else:
            _print_seed_summary(summary)

        counts = gather_counts(db)
        _print_counts(counts)

        if args.commit:
            try:
                db.commit()
            except SQLAlchemyError as exc:
                print(
                    f"error: commit failed against "
                    f"{_target_description(database_url)} "
                    f"({type(exc).__name__})",
                    file=sys.stderr,
                )
                return 1
            print("COMMITTED")
        else:
            db.rollback()
            print("DRY RUN — nothing written")
    finally:
        try:
            db.close()
        except SQLAlchemyError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
