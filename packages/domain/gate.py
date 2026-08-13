"""The deterministic eligibility gate (OC-37 §5): pass/fail/skip in code, zero
model involvement (OC-5, OC-22), consuming the user_policies seam and the
canonical profile.

Every dimension returns a verdict with its reason (the CompCheck pattern
generalized), every skip is visible in gate output, and a skip never excludes:
the overall verdict fails only on a decisive structured conflict in at least
one dimension. Work authorization and language are explicitly inactive in V0:
no ratified canonical representation exists, so both always skip with that
stated reason rather than inventing an interpretation.
"""

from dataclasses import dataclass, field
from typing import Literal

from domain.normalization import (
    CanonicalLocation,
    SENIORITY_BANDS,
    normalize_location,
    normalize_seniority,
)
from domain.policies import CompVerdict, check_compensation_floor

DimensionName = Literal[
    "compensation", "location", "timezone", "industry_exclusion",
    "company_stage_exclusion", "company_size_exclusion", "seniority",
    "work_authorization", "language",
]


@dataclass(frozen=True)
class DimensionCheck:
    """One dimension's verdict, reason always stated."""

    dimension: DimensionName
    verdict: CompVerdict
    reason: str
    note: str | None = None


@dataclass(frozen=True)
class GateResult:
    """The whole gate's output: overall pass/fail plus every dimension with
    its reason, stored auditable because the gate deletes most of the funnel."""

    verdict: Literal["pass", "fail"]
    dimensions: tuple[DimensionCheck, ...]

    def dimension(self, name: str) -> DimensionCheck:
        for check in self.dimensions:
            if check.dimension == name:
                return check
        raise KeyError(name)


@dataclass(frozen=True)
class PostingFacts:
    """The structurally stated facts the gate consumes, already normalized by
    the adapter layer where shapes allow (salary via normalize_salary,
    seniority via normalize_seniority). Raw location strings normalize here at
    gate time through the same tested table as the user side."""

    locations: tuple[str, ...] = ()
    remote_mode: Literal["onsite", "hybrid", "remote"] | None = None
    # None = no stated jurisdiction restriction; a stated list is normalized
    # here, and an entry failing normalization makes the restriction
    # unresolvable (skip, never a guess).
    remote_restriction_countries: tuple[str, ...] | None = None
    # Structurally stated timezone requirement only, in the timezone_bounds
    # shape; free text never reaches this field.
    timezone_requirement: dict | None = None
    salary: dict | None = None
    seniority: str | None = None


@dataclass(frozen=True)
class CompanyMetadata:
    """Reviewed registry metadata (§2): populated by curation or explicit CLI
    edit only; unknown skips, and no classifier fills these silently."""

    industry: str | None = None
    company_stage: str | None = None
    company_size_band: str | None = None


@dataclass(frozen=True)
class GateContext:
    """The user side: policies (the seam's dict), residence country raw string
    from the profile, and the active role families' target_seniority values."""

    policies: dict = field(default_factory=dict)
    residence_country: str | None = None
    active_family_target_seniorities: tuple[str | None, ...] = ()
    company: CompanyMetadata = field(default_factory=CompanyMetadata)


_INACTIVE_REASON = ("dimension is explicitly inactive in V0: no ratified canonical"
                    " representation exists; skipped, never inferred")


def _normalize_user_countries(context: GateContext) -> tuple[set[str], list[str]]:
    """Whitelist entries plus residence through the same lookup table the
    posting side uses. Returns (allowed country codes, unnormalizable raws)."""
    allowed: set[str] = set()
    failures: list[str] = []
    for raw in context.policies.get("relocation_whitelist") or []:
        location = normalize_location(raw)
        if location is None:
            failures.append(raw)
        else:
            allowed.add(location.country)
    if context.residence_country is not None:
        residence = normalize_location(context.residence_country)
        if residence is None:
            failures.append(context.residence_country)
        else:
            allowed.add(residence.country)
    return allowed, failures


def _user_cities_in(context: GateContext, country: str) -> set[str]:
    cities = set()
    for raw in context.policies.get("relocation_whitelist") or []:
        location = normalize_location(raw)
        if location is not None and location.country == country and location.city:
            cities.add(location.city)
    return cities


def _check_location(posting: PostingFacts, context: GateContext) -> DimensionCheck:
    """The §5 comparison matrix, exactly: country level is decisive; city
    granularity only when both sides share the country with cities present,
    and a city mismatch skips with a note (relocation within a listed country
    is a human judgment). A fail requires a decisive structured conflict."""
    allowed, failures = _normalize_user_countries(context)
    if failures:
        return DimensionCheck(
            "location", "skip",
            f"user-side location(s) failed normalization: {failures};"
            " location check skipped, never guessed")
    if not allowed:
        return DimensionCheck(
            "location", "skip",
            "no relocation whitelist or residence country set; location check skipped")

    if posting.remote_mode == "remote":
        if posting.remote_restriction_countries is None:
            return DimensionCheck(
                "location", "pass", "unrestricted remote posting passes the location dimension")
        restriction: set[str] = set()
        for raw in posting.remote_restriction_countries:
            location = normalize_location(raw)
            if location is None:
                # Neutral by design: fetched posting text never interpolates
                # into a CLI-visible reason.
                return DimensionCheck(
                    "location", "skip",
                    "a remote jurisdiction restriction entry could not be"
                    " normalized; restriction unresolvable, location check"
                    " skipped")
            restriction.add(location.country)
        if restriction & allowed:
            return DimensionCheck(
                "location", "pass",
                "remote jurisdiction restriction includes an allowed country")
        return DimensionCheck(
            "location", "fail",
            "remote jurisdiction restriction excludes the residence country and"
            f" every whitelist country (restriction: {sorted(restriction)})")

    if posting.remote_mode in ("onsite", "hybrid") or posting.locations:
        if not posting.locations:
            return DimensionCheck(
                "location", "skip",
                f"{posting.remote_mode} posting states no base location;"
                " location check skipped")
        normalized: list[CanonicalLocation] = []
        for raw in posting.locations:
            location = normalize_location(raw)
            if location is None:
                # Neutral by design: fetched posting text never interpolates
                # into a CLI-visible reason.
                return DimensionCheck(
                    "location", "skip",
                    "posting location could not be normalized; skipped")
            normalized.append(location)
        matches = [loc for loc in normalized if loc.country in allowed]
        if not matches:
            return DimensionCheck(
                "location", "fail",
                "every stated location's country is outside the whitelist countries"
                f" and the residence country ({sorted({loc.country for loc in normalized})})")
        for match in matches:
            user_cities = _user_cities_in(context, match.country)
            if not match.city or not user_cities or match.city in user_cities:
                return DimensionCheck(
                    "location", "pass",
                    f"stated location country {match.country} is allowed")
        return DimensionCheck(
            "location", "skip",
            "location is in a whitelisted country but no listed city matches",
            note="city mismatch inside a whitelisted country: relocation within"
                 " a listed country is a human judgment")

    return DimensionCheck(
        "location", "skip",
        "posting states no location or remote mode; location check skipped")


def _check_timezone(posting: PostingFacts, context: GateContext) -> DimensionCheck:
    """Only a structurally stated timezone requirement compares against
    timezone_bounds; anything else skips."""
    bounds = context.policies.get("timezone_bounds")
    requirement = posting.timezone_requirement
    if requirement is None:
        return DimensionCheck(
            "timezone", "skip", "posting states no structured timezone requirement")
    if bounds is None:
        return DimensionCheck(
            "timezone", "skip", "no timezone_bounds policy set; timezone check skipped")
    required = (requirement.get("min_utc_offset"), requirement.get("max_utc_offset"))
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in required):
        return DimensionCheck(
            "timezone", "skip",
            "posting timezone requirement is malformed; timezone check skipped")
    if required[0] > bounds["max_utc_offset"] or required[1] < bounds["min_utc_offset"]:
        return DimensionCheck(
            "timezone", "fail",
            f"posting timezone requirement UTC{required[0]:+d}..{required[1]:+d} is"
            " disjoint from the user's timezone bounds")
    return DimensionCheck(
        "timezone", "pass", "posting timezone requirement overlaps the user's bounds")


def _check_exclusion(dimension: DimensionName, value: str | None, out_list,
                     what: str) -> DimensionCheck:
    """Hard exclusions match deterministically against reviewed registry
    metadata only; unknown or unreviewed metadata skips, never fails."""
    if not out_list:
        return DimensionCheck(dimension, "skip", f"no {what} exclusions set")
    if value is None:
        return DimensionCheck(
            dimension, "skip",
            f"company {what} is not reviewed registry metadata; exclusion skipped,"
            " never classified silently")
    normalized_out = {str(entry).strip().lower() for entry in out_list}
    if value.strip().lower() in normalized_out:
        return DimensionCheck(
            dimension, "fail", f"company {what} '{value}' matches a hard exclusion")
    return DimensionCheck(
        dimension, "pass", f"company {what} '{value}' matches no exclusion")


def _check_seniority(posting: PostingFacts, context: GateContext) -> DimensionCheck:
    """Posting band against the active role families' target_seniority values,
    both sides through the same tables; a family yielding no band contributes
    no constraint, and only a decisive mismatch fails."""
    if posting.seniority is None or posting.seniority not in SENIORITY_BANDS:
        return DimensionCheck(
            "seniority", "skip",
            "posting seniority is unstated or unnormalizable; seniority check skipped")
    target_bands = set()
    for target in context.active_family_target_seniorities:
        band = normalize_seniority(structured=target, title=target)
        if band is not None:
            target_bands.add(band)
    if not target_bands:
        return DimensionCheck(
            "seniority", "skip",
            "no active role family yields a seniority band; seniority check skipped")
    if posting.seniority in target_bands:
        return DimensionCheck(
            "seniority", "pass",
            f"posting seniority '{posting.seniority}' matches an active family's target")
    return DimensionCheck(
        "seniority", "fail",
        f"posting seniority '{posting.seniority}' is outside every active"
        f" family's band ({sorted(target_bands)})")


def evaluate_gate(posting: PostingFacts, context: GateContext) -> GateResult:
    """Run every dimension, return the overall verdict with all reasons. A
    skip never excludes: overall fail requires at least one failing dimension."""
    comp = check_compensation_floor(
        posting.salary, context.policies.get("compensation_floor"))
    prefs = context.policies
    dimensions = (
        DimensionCheck("compensation", comp.verdict, comp.reason, comp.note),
        _check_location(posting, context),
        _check_timezone(posting, context),
        _check_exclusion("industry_exclusion", context.company.industry,
                         (prefs.get("industry_pref") or {}).get("out"), "industry"),
        _check_exclusion("company_stage_exclusion", context.company.company_stage,
                         (prefs.get("company_stage_pref") or {}).get("out"), "stage"),
        _check_exclusion("company_size_exclusion", context.company.company_size_band,
                         (prefs.get("company_size_pref") or {}).get("out"), "size band"),
        _check_seniority(posting, context),
        DimensionCheck("work_authorization", "skip", _INACTIVE_REASON),
        DimensionCheck("language", "skip", _INACTIVE_REASON),
    )
    verdict = "fail" if any(d.verdict == "fail" for d in dimensions) else "pass"
    return GateResult(verdict=verdict, dimensions=dimensions)
