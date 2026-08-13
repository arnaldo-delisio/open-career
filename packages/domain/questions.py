"""Typed question registry (OC-35; spec: the scope's
decisions/onboarding-interview-design.md, "Question inventory and sequencing").

Every canonical profile field (OC-29) and every policy key (OC-35) has exactly
one registry entry carrying its intentional disposition: TIER1 (sitting one,
the must-ask block after the CV walk and families step), TIER2 (`open-career
deepen`), DEPTH (`open-career stories`, preferences/logistics clusters), or
FLOOR (deliberately never interviewed; surfaces as a named manual action when
an application needs it, a Resolution once OC-6 ships). The interview flows are
generated from this registry; a completeness test asserts the coverage, so a
schema field can never silently gain no question.

Validation is the seam itself: profile answers write through the audited
profile seam (shape checks in domain/profile.py), policy answers through the
audited policy seam (shape checks in domain/policies.py). `choices` narrows
free-text prompts where the consumer expects an enum-ish answer; it is a
prompt aid, not a substitute for seam validation.
"""

from dataclasses import dataclass
from typing import Literal

from domain.policies import CANONICAL_POLICY_KEYS
from domain.profile import CANONICAL_PROFILE_FIELDS, YES_NO_CHOICES

QuestionKind = Literal["profile", "policy"]
Tier = Literal["TIER1", "TIER2", "DEPTH", "FLOOR"]

TIER1: Tier = "TIER1"
TIER2: Tier = "TIER2"
DEPTH: Tier = "DEPTH"
FLOOR: Tier = "FLOOR"


@dataclass(frozen=True)
class Question:
    """One field or policy's interview disposition. Skip semantics are uniform
    (spec): every question shows its current value, blank is skip, and a
    skipped item stays empty and falls to the progressive floor; skip is never
    a decline (EEO decline is the stance policy, structural, never a blank)."""

    key: str
    kind: QuestionKind
    tier: Tier
    prompt: str
    consumer: str  # who reads the answer, so every ask earns its time
    choices: tuple[str, ...] = ()


_IDENTITY_CONTACT = (
    Question("full_name", "profile", TIER1, "Full name",
             "identity block on every application form"),
    Question("email", "profile", TIER1, "Email",
             "contact block on every application form"),
    Question("phone", "profile", TIER1, "Phone",
             "contact block on every application form"),
    Question("location", "profile", TIER1, "Location (city)",
             "location block; eligibility gate location check"),
    Question("country", "profile", TIER1, "Country",
             "location block; work-authorization templating per geography"),
)

_TIER1_LOGISTICS = (
    Question("authorized_in_country", "profile", TIER1,
             "Authorized to work in your target country?",
             "work-authorization question; eligibility gate", choices=YES_NO_CHOICES),
    Question("needs_sponsorship", "profile", TIER1,
             "Will you need visa sponsorship?",
             "sponsorship question; eligibility gate", choices=YES_NO_CHOICES),
    Question("remote_preference", "profile", TIER1,
             "Remote preference (e.g. remote / hybrid / onsite)",
             "remote-policy question; eligibility gate"),
    Question("relocation", "profile", TIER1,
             "Willing to relocate?",
             "relocation question; eligibility gate", choices=YES_NO_CHOICES),
    Question("notice_period", "profile", TIER1,
             "Notice period (e.g. 1 month)",
             "availability question on application forms"),
)

_TIER1_POLICIES = (
    Question("compensation_floor", "policy", TIER1,
             "Compensation floor (below this a posting auto-fails)",
             "eligibility gate comp check (deterministic, OC-22)"),
    Question("compensation_target", "policy", TIER1,
             "Compensation target range, plus which value a single-number"
             " salary field receives",
             "salary fields: range forms get the range, scalar forms the chosen value"),
)

_TIER2_PROFILE = (
    Question("preferred_name", "profile", TIER2, "Preferred name",
             "identity block where forms ask it"),
    Question("pronouns", "profile", TIER2, "Pronouns",
             "identity block where forms ask it"),
    Question("linkedin_url", "profile", TIER2, "LinkedIn URL",
             "links block on application forms"),
    Question("github_url", "profile", TIER2, "GitHub URL",
             "links block on application forms"),
    Question("portfolio_url", "profile", TIER2, "Portfolio URL",
             "links block on application forms"),
    Question("website_url", "profile", TIER2, "Website URL",
             "links block on application forms"),
    Question("current_company", "profile", TIER2, "Current company",
             "logistics block on application forms"),
    Question("current_title", "profile", TIER2, "Current title",
             "logistics block on application forms"),
    Question("privacy_consent", "profile", TIER2,
             "Standing privacy-policy consent statement",
             "consent checkboxes where forms carry them"),
    Question("future_contact_consent", "profile", TIER2,
             "Standing future-contact consent",
             "talent-pool consent where forms carry it", choices=YES_NO_CHOICES),
    # EEO answers fill platform-templated blocks only under stance
    # answer_honestly; empty fields plus the stance are structural skip vs
    # decline, never ambiguous blanks (spec, eeo_stance).
    Question("eeo_gender", "profile", TIER2, "EEO: gender",
             "EEO block, only under eeo_stance=answer_honestly"),
    Question("eeo_hispanic_latino", "profile", TIER2, "EEO: Hispanic/Latino",
             "EEO block, only under eeo_stance=answer_honestly"),
    Question("eeo_race_ethnicity", "profile", TIER2, "EEO: race/ethnicity",
             "EEO block, only under eeo_stance=answer_honestly"),
    Question("eeo_veteran", "profile", TIER2, "EEO: veteran status",
             "EEO block, only under eeo_stance=answer_honestly"),
    Question("eeo_disability", "profile", TIER2, "EEO: disability status",
             "EEO block, only under eeo_stance=answer_honestly"),
    Question("eeo_orientation", "profile", TIER2, "EEO: sexual orientation",
             "EEO block, only under eeo_stance=answer_honestly"),
    Question("eeo_age_band", "profile", TIER2, "EEO: age band",
             "EEO block, only under eeo_stance=answer_honestly"),
)

_TIER2_POLICIES = (
    Question("never_render", "policy", TIER2,
             "Strings that must never appear in any rendered artifact"
             " (personal boundaries, e.g. a name you never publish)",
             "Gauntlet stage-zero user-constraints check, via the policy"
             " snapshot; the list lives in instance data, the check in code"),
    Question("eeo_stance", "policy", TIER2,
             "EEO stance: answer honestly, always decline, or decide per application",
             "routes every EEO block; unset behaves as per_application",
             choices=("answer_honestly", "always_decline", "per_application")),
)

_FLOOR_PROFILE = (
    # Compensation state lives in the floor/target policies; this canonical
    # field is deliberately never interviewed (a second ask would mint a rival
    # home for the same fact). It stays writable via `profile set` and falls
    # to the progressive floor where a form wants literal free text.
    Question("salary_expectation", "profile", FLOOR,
             "Salary expectation (free text)",
             "salary free-text fields; derived from the compensation policies"
             " at fill time where possible, manual action otherwise"),
)

_DEPTH_POLICIES = (
    Question("company_stage_pref", "policy", DEPTH,
             "Company stage preferences (in/out lists)",
             "'out' feeds the eligibility gate; 'in' is ranking context only"),
    Question("company_size_pref", "policy", DEPTH,
             "Company size preferences (in/out lists)",
             "'out' feeds the eligibility gate; 'in' is ranking context only"),
    Question("industry_pref", "policy", DEPTH,
             "Industry preferences (in/out lists)",
             "'out' feeds the eligibility gate; 'in' is ranking context only"),
    Question("work_track", "policy", DEPTH, "Work track",
             "ranking context; never answers a company-specific question",
             choices=("ic", "management", "either")),
    Question("mission_themes", "policy", DEPTH, "Mission themes that matter to you",
             "ranking context only"),
    Question("relocation_whitelist", "policy", DEPTH,
             "Cities you would relocate to",
             "eligibility gate location check refinement"),
    Question("timezone_bounds", "policy", DEPTH,
             "Acceptable timezone bounds (UTC offsets)",
             "eligibility gate for remote roles with timezone requirements"),
    Question("visa_details", "policy", DEPTH,
             "Visa details (status note, expiry)",
             "work-authorization detail questions"),
    Question("earliest_start", "policy", DEPTH, "Earliest start date",
             "availability questions on application forms"),
)

QUESTION_REGISTRY: tuple[Question, ...] = (
    _IDENTITY_CONTACT + _TIER1_LOGISTICS + _TIER1_POLICIES
    + _TIER2_PROFILE + _TIER2_POLICIES + _FLOOR_PROFILE + _DEPTH_POLICIES
)


def questions(kind: QuestionKind | None = None, tier: Tier | None = None) -> tuple[Question, ...]:
    """The registry filtered by kind and/or tier, in registry (ask) order."""
    return tuple(q for q in QUESTION_REGISTRY
                 if (kind is None or q.kind == kind) and (tier is None or q.tier == tier))


def registry_coverage() -> tuple[set[str], set[str]]:
    """(profile field keys, policy keys) present in the registry; the
    completeness test asserts these equal the canonical sets exactly."""
    return ({q.key for q in QUESTION_REGISTRY if q.kind == "profile"},
            {q.key for q in QUESTION_REGISTRY if q.kind == "policy"})


def _assert_registry_is_complete() -> None:
    profile_keys, policy_keys = registry_coverage()
    if profile_keys != CANONICAL_PROFILE_FIELDS or policy_keys != CANONICAL_POLICY_KEYS:
        raise AssertionError("question registry does not cover the canonical sets")
    if len(QUESTION_REGISTRY) != len(profile_keys) + len(policy_keys):
        raise AssertionError("question registry carries a duplicate key")


_assert_registry_is_complete()  # import-time backstop; the real test is in tests/
