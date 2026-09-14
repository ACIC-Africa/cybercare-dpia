# The Kenyan oil-marketer taxonomy, as data.
#
# Every customer term from 06_oil_marketer_process_and_data_map.json is a row
# below, with the decision already made: reuse an existing Fides key, create
# a new one, tag an existing default row (special-category carrier), or defer
# to Carol because the term isn't decidable from the workbook alone. This
# module makes no database calls and runs no logic beyond the count asserts
# that keep it honest — loader.py is the only place that touches SQL.
from dataclasses import dataclass
from typing import Optional

SOURCE = "06_oil_marketer_process_and_data_map.json"

# D-KT-6: every subject we load gets the same six rights, uniformly. Not a
# per-subject judgment call; provenance recorded below so the "why" survives.
SIX_RIGHTS = {
    "strategy": "INCLUDE",
    "values": [
        "Access",
        "Rectification",
        "Erasure",
        "Restrict Processing",
        "Object",
        "Portability",
    ],
}
RIGHTS_PROVENANCE = "01_brief_for_dpia.md §3"

# DPA 2019's own vocabulary for the tag we carry on special-category rows
# (both tagged defaults and newly created leaves).
SPECIAL_TAG = "dpa2019:special_category"


@dataclass(frozen=True)
class Subject:
    term: str  # customer's spelling, verbatim
    fides_key: Optional[str]  # None for dropped
    action: str  # reuse | create | dropped
    reason: str
    names_person_directly: Optional[bool]  # None = ambiguous → Carol
    natural_person_role: Optional[str]


@dataclass(frozen=True)
class Category:
    term: str
    fides_key: Optional[str]
    parent_key: Optional[str]  # required when action == "create"
    action: str  # reuse | create | tag | not_personal_data | deferred
    special: bool  # in the customer's "Special Categories" bucket
    reason: str
    deferred_to: Optional[str] = None


@dataclass(frozen=True)
class Ground:
    ground: str
    source_class: Optional[str]
    fides_legal_basis: Optional[str]
    suggested: Optional[str]


# All 43 subjects.
SUBJECTS = [
    Subject("Applicant", "applicant", "create", "Not job_applicant: grounds list has 'Enrolment of an Applicant' (dealer enrolment). Ambiguous natural/institutional.", None, None),
    Subject("Agent", "agent", "create", "No Fides key. Ambiguous natural/institutional.", None, None),
    Subject("Auditor", "auditor", "create", "No Fides key.", True, "auditor"),
    Subject("Authorized Signatory", "authorized_signatory", "create", "KYC axis (spec D-KT-8).", True, "signatory"),
    Subject("Banking and Other Financial Institutions", "banking_financial_institution", "create", "Institutional; humans reached via relationship staff.", False, None),
    Subject("Beneficiary", "beneficiary", "create", "No Fides key. Ambiguous (person or entity).", None, None),
    Subject("Board Member", "board_member", "create", "No Fides key.", True, "board member"),
    Subject("Candidate", "job_applicant", "reuse", "Recruitment candidate = Fides job_applicant.", True, "candidate"),
    Subject("Child/Minor", "child_minor", "create", "Fides has no child subject; assessment has a Children Consent theme.", True, "child"),
    Subject("Complainant/Enquirer", "complainant_enquirer", "create", "No Fides key.", True, "complainant"),
    Subject("Consultant", "consultant", "reuse", "Exact.", True, "consultant"),
    Subject("Contracting Party", "contracting_party", "create", "KYC axis; institutional.", False, None),
    Subject("Customer/Client", "customer", "reuse", "Fides customer; name updated to customer's term. Corporate or natural: ambiguous.", None, None),
    Subject("Debt Collector", "debt_collector", "create", "No Fides key. Ambiguous (agency or individual).", None, None),
    Subject("Dependant", "dependant", "create", "No Fides key.", True, "dependant"),
    Subject("Director", "director", "create", "No Fides key.", True, "director"),
    Subject("Emergency Contact/Next of Kin", "next_of_kin", "reuse", "Fides next_of_kin; name updated. See OQ-KT-2 for the category reading.", True, "next of kin"),
    Subject("Employee", "employee", "reuse", "Exact.", True, "employee"),
    Subject("Employer", "employer", "create", "Institutional (the counterparty employing a data subject).", False, None),
    Subject("Expatriate", "expatriate", "create", "No Fides key; an employee class with immigration data.", True, "employee"),
    Subject("External Company/Contractor", "external_company_contractor", "create", "Institutional; staff on site are the humans.", False, None),
    Subject("Financial Advisor/Sales Consultants", "financial_advisor_sales_consultant", "create", "No Fides key.", True, "sales agent"),
    Subject("Independent Contractor", "independent_contractor", "create", "No Fides key.", True, "contractor"),
    Subject("Individuals Captured by CCTV Images", "cctv_captured_individual", "create", "No Fides key.", True, "person on premises"),
    Subject("Intermediary/Agent/Independent Broker", "intermediary_broker", "create", "Compound term; institutional.", False, None),
    Subject("Law Firm/Advocate", "law_firm_advocate", "create", "Institutional; the advocate on the matter is the human.", False, None),
    Subject("Member", "member", "create", "No Fides key. Ambiguous (of what).", None, None),
    Subject("N/A", None, "dropped", "Workbook placeholder, not a subject.", None, None),
    Subject("Offender/Suspected Offender", "offender_suspected_offender", "create", "No Fides key.", True, "suspect"),
    Subject("Prospect", "prospect", "reuse", "Exact. Corporate or natural: ambiguous.", None, None),
    Subject("Shareholder/Investor", "shareholder", "reuse", "Fides shareholder; name updated. Institutional or natural.", False, None),
    Subject("Subsidiary Company", "subsidiary_company", "create", "Institutional.", False, None),
    Subject("Supplier/Service Provider", "supplier_vendor", "reuse", "Fides supplier_vendor; name updated.", False, None),
    Subject("Tenants", "tenant", "create", "No Fides key.", True, "tenant"),
    Subject("Landlord", "landlord", "create", "No Fides key. Ambiguous (person or company).", None, None),
    Subject("Trustee", "trustee", "create", "No Fides key. Ambiguous (person or corporate trustee).", None, None),
    Subject("Valuer", "valuer", "create", "No Fides key. Ambiguous (firm or individual).", None, None),
    Subject("Vendor/Supplier", "vendor_supplier", "create", "Customer keeps Vendor/Supplier distinct from Supplier/Service Provider; Fides does not. Own key so the distinction survives.", False, None),
    Subject("Visitors", "visitor", "reuse", "Fides visitor; name updated.", True, "visitor"),
    Subject("Witness", "witness", "create", "No Fides key.", True, "witness"),
    Subject("Government Agency", "government_agency", "create", "Institutional.", False, None),
    Subject("Government Agency & Staff", "government_agency_staff", "create", "Compound: names the officer through the agency.", True, "officer"),
    Subject("Governmenet Agency & Client", "government_agency_client", "create", "Compound: names the client through the agency. Source spelling 'Governmenet' preserved in name (sic; suggested correction 'Government').", True, "client"),
]
assert len(SUBJECTS) == 43
assert sum(s.action == "reuse" for s in SUBJECTS) == 9
assert sum(s.action == "create" for s in SUBJECTS) == 33
assert sum(s.names_person_directly is True for s in SUBJECTS) == 21
assert sum(s.names_person_directly is False for s in SUBJECTS) == 11
assert sum(s.names_person_directly is None and s.action != "dropped" for s in SUBJECTS) == 10

# All 68 categories. Special first (28), then the 40 others.
H = "user.health_and_medical"
D = "user.demographic"
P = "user.content.private"
CATEGORIES = [
    # --- Special Categories of Personal Data (28) ---
    Category("Race", "user.demographic.race_ethnicity", None, "tag", True, "Exact Fides leaf; tagged."),
    Category("Gender", "user.demographic.gender", None, "tag", True, "Exact; tagged."),
    Category("Pregnancy Status", f"{H}.pregnancy_status", H, "create", True, "No Fides key; health parent."),
    Category("Marital Status", "user.demographic.marital_status", None, "tag", True, "Exact; tagged."),
    Category("Ethnic or Social Origin", "user.demographic.race_ethnicity", None, "tag", True, "Same Fides leaf as Race; both terms recorded."),
    Category("Sexual Orientation", "user.demographic.sexual_orientation", None, "tag", True, "Exact; tagged."),
    Category("Religion", "user.demographic.religious_belief", None, "tag", True, "Fides religious_belief; tagged."),
    Category("Conscience", f"{D}.conscience", D, "create", True, "Act lists conscience separately from belief; own leaf."),
    Category("Belief", f"{D}.belief", D, "create", True, "Act lists belief separately from religion; own leaf, not folded into religious_belief."),
    Category("Physical Health", f"{H}.physical_health", H, "create", True, "Parent exists, no leaf."),
    Category("Mental Health", f"{H}.mental_health", H, "create", True, "Parent exists, no leaf."),
    Category("Well-being", f"{H}.well_being", H, "create", True, "Parent exists, no leaf."),
    Category("Disability", f"{H}.disability", H, "create", True, "Parent exists, no leaf."),
    Category("Injuries sustained in an accident", f"{H}.accident_injuries", H, "create", True, "Parent exists, no leaf."),
    Category("Medical History", f"{H}.medical_history", H, "create", True, "Parent exists, no leaf."),
    Category("Medical Condition and Treatment", f"{H}.condition_and_treatment", H, "create", True, "Parent exists, no leaf."),
    Category("Criminal History", "user.criminal_history", None, "tag", True, "Exact; tagged."),
    Category("Symbol", None, None, "deferred", True, "Meaning not discernible from the workbook; not invented.", deferred_to="Carol"),
    Category("Biometric Information", "user.biometric", None, "tag", True, "Exact parent; tagged (children inherit by hierarchy)."),
    Category("Personal Opinions, Views or Preferences", f"{P}.personal_opinions", P, "create", True, "Content the subject authored; private content parent."),
    Category("Correspondence", f"{P}.correspondence", P, "create", True, "Private content parent."),
    Category("Views/opinions of another individual about the DS", f"{D}.opinions_about_subject", D, "create", True, "No parent describes 'about the subject by others'; demographic holds Fides' profile/opinion attributes. Cheap to move."),
    Category("Trade Union Membership", f"{D}.trade_union_membership", D, "create", True, "No Fides key."),
    Category("Political Persuasion", "user.demographic.political_opinion", None, "tag", True, "Fides political_opinion; tagged."),
    Category("Next of Kin or Familial Relationship", f"{D}.family_relationship", D, "create", True, "Category reading of next of kin; DPA 2019 s.2 includes 'family details' in sensitive personal data. Subject reading kept too (OQ-KT-2 stays open, both loaded)."),
    Category("Nature of Relationship to Data Subject", f"{D}.relationship_to_subject", D, "create", True, "No Fides key."),
    Category("HIV Status", f"{H}.hiv_status", H, "create", True, "Own leaf; statutory weight in Kenya (spec)."),
    Category("Information that could affect health/ability to work", f"{H}.ability_to_work", H, "create", True, "Parent exists, no leaf."),
    # --- Identifier Information (10) ---
    Category("Name", "user.name", None, "reuse", False, "Exact."),
    Category("Surname", "user.name.last", None, "reuse", False, "Exact."),
    Category("Identification Numbers", "user.government_id", None, "reuse", False, "Parent."),
    Category("ID NO/PP NO/", "user.government_id.national_identification_number", None, "reuse", False, "Compound ID/passport; national id chosen, passport_number exists for precise use."),
    Category("Driving License No, KRA PIN No", "user.government_id.tax_pin", "user.government_id", "create", False, "Compound; drivers_license_number exists in Fides, KRA PIN does not — new leaf for the PIN, term mapped to it."),
    Category("Certificate of Incorporation or license number/ patents, Intellectual rights, logos, brand/ trademark", None, None, "deferred", False, "Corporate identifiers; personal data only when the registrant is a natural person.", deferred_to="Carol"),
    Category("Incorporation or license number/ patents, Intellectual rights, logos, brand Names /Trade mark", None, None, "deferred", False, "Duplicate of the previous term; same ruling.", deferred_to="Carol"),
    Category("Symbols and Signature", "user.unique_id.signature", "user.unique_id", "create", False, "A signature identifies a person; no Fides key."),
    Category("Online Identifier/Assignment to Person", "user.unique_id", None, "reuse", False, "Fides unique_id."),
    Category("Online Identifier/Assignment to Person/Symbols", "user.unique_id", None, "reuse", False, "Duplicate term; same key, both recorded."),
    # --- Contact Information (5) ---
    Category("Contact Details", "user.contact", None, "reuse", False, "Parent."),
    Category("Email Address", "user.contact.email", None, "reuse", False, "Exact."),
    Category("Physical Address", "user.contact.address.street", None, "reuse", False, "Exact."),
    Category("Postal Address and code", "user.contact.address.postal_code", None, "reuse", False, "Exact."),
    Category("Telephone Number", "user.contact.phone_number", None, "reuse", False, "Exact."),
    # --- Date of Birth & Age (2) ---
    Category("Age", "user.demographic.age_range", None, "reuse", False, "Nearest Fides leaf."),
    Category("Date of Birth", "user.demographic.date_of_birth", None, "reuse", False, "Exact."),
    # --- Economic/Financial/ Property (5) ---
    Category("Financial Information", "user.financial", None, "reuse", False, "Parent."),
    Category("Property Details", "user.financial.property", "user.financial", "create", False, "No Fides key. DPA 2019 s.2 (corpus doc 09) lists 'property details' in sensitive personal data; the customer's bucket says non-special. Loaded as the customer has it, untagged. OQ-KT-5 → Carol."),
    Category("Annual Income", "user.financial.income", "user.financial", "create", False, "No Fides key."),
    Category("Occupation/Business", "user.job_title", None, "reuse", False, "Nearest Fides leaf."),
    Category("Information on past account transactions", "user.financial.transaction_history", "user.financial", "create", False, "Fides purchase_history is behavioural; this is financial."),
    # --- Education and Professional (1) / Employment (1) / Location (1) ---
    Category("Education", "user.education", "user", "create", False, "No Fides key."),
    Category("Employment History", "user.workplace.employment_history", "user.workplace", "create", False, "Fides workplace is current employer only."),
    Category("Location Information", "user.location", None, "reuse", False, "Parent."),
    # --- Operation Information (6) / Reports (1): not personal data ---
    Category("Operation Information", None, None, "not_personal_data", False, "Organisational document type."),
    Category("Procedure/Process", None, None, "not_personal_data", False, "Organisational document type."),
    Category("Policy", None, None, "not_personal_data", False, "Organisational document type."),
    Category("Organization charts", None, None, "not_personal_data", False, "Organisational document type (names in it are covered by Name)."),
    Category("Product or Service Information-Factsheet", None, None, "not_personal_data", False, "Organisational document type."),
    Category("Research & Publications", None, None, "not_personal_data", False, "Organisational document type."),
    Category("Reports", None, None, "not_personal_data", False, "Organisational document type."),
    # --- Contracts (1) / Resolutions (2) ---
    Category("Contracts, Leases, Examples include purchase agreement, lease, land agreement, Employment agreement, Supplier/Vendor Agreement.", f"{P}.contracts", P, "create", False, "A contract with a person carries personal data."),
    Category("Meeting Board Resolution", None, None, "deferred", False, "Names directors; whether treated as personal data record is the engagement's call.", deferred_to="Carol"),
    Category("Judicial decision/Judgements", None, None, "deferred", False, "May be criminal_history (special) or civil; not decidable from the term.", deferred_to="Carol"),
    # --- Nationality (2) / Physiological (1) / Social Cultural (2) ---
    Category("Nationality", f"{D}.nationality", D, "create", False, "No Fides key."),
    Category("Country of Residence", "user.contact.address.country", None, "reuse", False, "Exact."),
    Category("Color", f"{D}.colour", D, "create", False, "Physiological colour; own leaf, not folded into race_ethnicity (customer buckets it as non-special)."),
    Category("Culture", f"{D}.culture", D, "create", False, "No Fides key."),
    Category("Language", "user.demographic.language", None, "reuse", False, "Exact."),
]
assert len(CATEGORIES) == 68
assert sum(c.special for c in CATEGORIES) == 28
assert sum(c.action == "tag" for c in CATEGORIES) == 9
assert len({c.fides_key for c in CATEGORIES if c.action == "tag"}) == 8  # Race + Ethnic origin share a key
assert sum(c.action == "create" and c.special for c in CATEGORIES) == 18
assert sum(c.action == "create" for c in CATEGORIES) == 29
assert sum(c.action == "deferred" for c in CATEGORIES) == 5
assert sum(c.action == "not_personal_data" for c in CATEGORIES) == 7

# All 23 grounds. `fides_legal_basis` comes ONLY from `source_class`;
# `suggested` only where the name is unambiguous.
GROUNDS = [
    Ground("Adherence to Pension / Collective Agreement Laws", "Legal", "Legal obligations", None),
    Ground("Background Checks and Pre-employment Screening", "Legal", "Legal obligations", None),
    Ground("Compliance with Measures / Laws to Redress Unfair Discrimination", "Consent", "Consent", None),
    Ground("Consent by a Child's Parent or Guardian", "Consent", "Consent", None),
    Ground("Consent by the Data Subject", "Consent", "Consent", None),
    Ground("Customer Relationship Administration", "Legitimate Interest", "Legitimate interests", None),
    Ground("Enrolment of an Applicant", "Legitimate Interest", "Legitimate interests", None),
    Ground("Establishment, exercise or defense of a legal claim (SPI)", "Legal basis", None, None),
    Ground("KYC Requirements", "Legitimate Interest", "Legitimate interests", None),
    Ground("Labour Legislation Compliance", "Legal", "Legal obligations", None),
    Ground("Legitimate Activities by a Foundation, Association or any other Not for Profit Body (SPI)", None, None, None),
    Ground("Legitimate Interest", None, None, "Legitimate interests"),
    Ground("Support Business Operation", None, None, None),
    Ground("Marketing", "Consent", "Consent", None),
    Ground("N/A", None, None, None),
    Ground("Obligation of Law (SPI)", "Legal", "Legal obligations", None),
    Ground("Obligation of Law- Legal basis", None, None, "Legal obligations"),
    Ground("Performance of a Contract(Provision of products and services)", None, None, "Contract"),
    Ground("Performance of an Insurance Agreement", None, None, "Contract"),
    Ground("Proper Medical Treatment / Care", None, None, None),
    Ground("Public Interest", None, None, "Public interest"),
    Ground("Research", None, None, None),
    Ground("Risk Assessment", None, None, None),
]
assert len(GROUNDS) == 23
assert sum(g.fides_legal_basis is not None for g in GROUNDS) == 11
