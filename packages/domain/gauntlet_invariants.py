"""Stage zero of the Gauntlet: deterministic invariant checks (spec: the
scope's decisions/gauntlet-design.md, "Stage zero"). Pure code, no model call,
running before any judge; any failure ends the run with zero model tokens
spent.

Input is the persisted audit bundle of one package version (snapshot bytes,
content model, stored artifact bytes and their extracted text) plus the policy
snapshot this module also defines: a write-once canonical serialization of
exactly the policy inputs the rules consume (the work-authorization projection
over the profile's closed yes/no strings, and the never_render list), so every
implementation judges the same bytes and a run never reads live mutable state.

Invariant dispositions are a three-value enum (pass, fail, attention),
distinct from the per-judge outcome enum: an attention invariant lets the
judges run but caps the run verdict at ATTENTION."""

import hashlib
import json
import re
from dataclasses import dataclass

from domain.ats_check import check_ats
from domain.cv_model import CvModel
from domain.dates import (
    MONTH_NAMES,
    chron_rank,
    is_readable,
    parse,
    starts_before_end,
)
from domain.grounding import GroundingVerifier
from domain.grounding_spec import SPEC_VERSION, normalize, normalize_chars

# Invariant dispositions (three-value, per the design).
PASS = "pass"
FAIL = "fail"
ATTENTION = "attention"

# Every verifier spec identifier this suite RECOGNIZES as a real spec, for
# audit-shape validation only. A stored report from a prior spec is a
# well-formed audit record, not a corrupt one: the semantic mismatch belongs
# to the regrounding rule, which caps the run at ATTENTION (the design's
# regrounding-unsupported path) while the judges still run. Treating a prior
# spec as an audit-integrity FAIL would end the run before that rule could
# ever return. An unrecognized identifier is still a FAIL: it attests nothing.
RECOGNIZED_VERIFIER_SPEC_VERSIONS = frozenset({"1", "2", "3"})
assert SPEC_VERSION in RECOGNIZED_VERIFIER_SPEC_VERSIONS

# The versioned deterministic work-authorization projection: detector
# vocabulary and phrase templates are part of this version; changing either
# bumps it (and with it the suite version).
WORK_AUTH_PROJECTION_VERSION = "wa-2"

# Detector vocabulary: a sentence matching any of these patterns (on
# grounding-spec normalized text, which is lowercased and whitespace
# collapsed) is an authorization-class assertion.
#
# These are employment-authorization PHRASES with word boundaries, never bare
# tokens. Bare tokens produce false stage-zero failures on ordinary grounded
# CV text ("Sponsored a community event", "citizen developer", "Sponsored the
# internal guild"), and a false blocking failure that rejects an honest
# package is worse here than a miss: the deterministic rule exists to stop a
# claim the user never made, not to police the word "sponsor".
_AUTH_PATTERNS = tuple(re.compile(p) for p in (
    # Authorization to work, in any of its usual orders.
    r"\bauthoriz(?:ed|ation)\b[^.]{0,40}\bto work\b",
    r"\bwork(?:ing)? authoriz(?:ed|ation)\b",
    r"\b(?:not|un)authoriz(?:ed)?\b[^.]{0,40}\bto work\b",
    r"\beligible to work\b",
    r"\bright to work\b",
    r"\bwork permit\b",
    r"\bworking rights?\b",
    # Visas and sponsorship, only in their immigration sense: a bare token
    # would fail an honest "visa free travel comparison feature".
    r"\bvisa sponsorship\b",
    r"\bwork visa\b",
    r"\bvisa (?:status|holder|required|sponsored)\b",
    r"\b(?:requires?|required|need(?:s|ed)?|no|without|holds?|holding|on a)\b"
    r"[^.]{0,20}\bvisas?\b",
    r"\bsponsorship\b[^.]{0,40}\b(?:visa|work|employment|required?|require[sd]?)\b",
    r"\b(?:require[sd]?|need(?:s|ed)?|seeking|no|without)\b[^.]{0,20}"
    r"\bsponsorship\b",
    r"\bsponsorship (?:is )?(?:not )?(?:required|needed)\b",
    # Status claims that assert authorization.
    r"\bgreen card\b",
    r"\bpermanent resident(?:cy|\b)",
    r"\b(?:eu|us|uk|dutch|italian|american|british)? ?citizen(?:ship)?\b"
    r"[^.]{0,40}\b(?:work|authoriz|employ|permit|status)",
    r"\bcitizenship status\b",
    r"\bwork(?:ing)? eligibility\b",
))

# Sentence boundaries in extracted artifact text: periods, newlines, form
# feeds; bullets are already whitespace after normalization.
_SENTENCE_SPLIT = re.compile(r"[.\n\f;]+")

# Date-like spans in rendered text: ISO YYYY-MM, a month name beside a year
# (the form a CV actually prints), or a standalone plausible year. Each hit is
# read through the canonical parser, so the horizon check sees "September 2015"
# as the month it is instead of only the bare year.
_MONTH_WORD = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
_RENDERED_DATE = re.compile(
    rf"\b(?:({_MONTH_WORD})\.?\s+((?:19|20)\d{{2}})"
    rf"|((?:19|20)\d{{2}})(?:-(0[1-9]|1[0-2]))?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class InvariantResult:
    rule: str
    disposition: str
    detail: str


def invariants_passed(results: tuple[InvariantResult, ...]) -> bool:
    return all(r.disposition != FAIL for r in results)


# -- the policy snapshot ------------------------------------------------------

def build_work_authorization_projection(profile: dict) -> dict:
    """The single canonical run input for work-authorization judging: the
    source field values copied verbatim from the profile (the closed yes/no
    strings onboarding persists), plus the derived allowed-forms list (a
    closed phrase template list). Absent fields derive an empty allowed set,
    so any rendered assertion fails."""
    authorized = profile.get("authorized_in_country")
    sponsorship = profile.get("needs_sponsorship")
    country = profile.get("country")
    forms: list[str] = []
    if authorized == "yes":
        forms.append("authorized to work")
        forms.append("legally authorized to work")
        if country:
            forms.append(f"authorized to work in {country}")
            forms.append(f"legally authorized to work in {country}")
    if sponsorship == "no":
        forms.append("no sponsorship required")
        forms.append("no visa sponsorship required")
        forms.append("does not require sponsorship")
        forms.append("does not require visa sponsorship")
    if sponsorship == "yes":
        forms.append("requires sponsorship")
        forms.append("requires visa sponsorship")
    return {
        "projection_version": WORK_AUTH_PROJECTION_VERSION,
        "authorized_in_country": authorized,
        "needs_sponsorship": sponsorship,
        "country": country,
        "allowed_forms": forms,
    }


def build_policy_snapshot(profile: dict, policies: dict) -> dict:
    """Exactly the policy inputs stage zero consumes, nothing else."""
    return {
        "work_authorization": build_work_authorization_projection(profile),
        "never_render": list(policies.get("never_render") or []),
    }


def policy_snapshot_json(snapshot: dict) -> str:
    """Canonical serialization: what gets written write-once and hashed."""
    return json.dumps(snapshot, indent=2, sort_keys=True)


class PolicySnapshotError(ValueError):
    """The persisted policy snapshot bytes fail the canonical schema."""


def parse_policy_snapshot(data: bytes) -> dict:
    """Decode and validate the persisted policy snapshot bytes against the
    canonical schema, fail closed. Stage zero consumes only what this
    returns, so every implementation judges the same persisted bytes."""
    try:
        snapshot = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PolicySnapshotError(f"policy snapshot is not valid JSON: {e}") from e
    if not isinstance(snapshot, dict) or set(snapshot) != {"work_authorization",
                                                           "never_render"}:
        raise PolicySnapshotError(
            "policy snapshot must be an object with exactly"
            " work_authorization and never_render")
    projection = snapshot["work_authorization"]
    if not isinstance(projection, dict) or set(projection) != {
            "projection_version", "authorized_in_country", "needs_sponsorship",
            "country", "allowed_forms"}:
        raise PolicySnapshotError("work_authorization projection has the wrong shape")
    if projection["projection_version"] != WORK_AUTH_PROJECTION_VERSION:
        raise PolicySnapshotError(
            f"unknown projection_version {projection['projection_version']!r}")
    if not isinstance(projection["allowed_forms"], list) or any(
            not isinstance(f, str) for f in projection["allowed_forms"]):
        raise PolicySnapshotError("allowed_forms must be a list of strings")
    # The closed source vocabulary: exactly the yes/no strings onboarding
    # persists, or null.
    for field in ("authorized_in_country", "needs_sponsorship"):
        if projection[field] not in (None, "yes", "no"):
            raise PolicySnapshotError(
                f"{field} must be null, 'yes' or 'no'; found"
                f" {projection[field]!r}")
    country = projection["country"]
    if country is not None and (not isinstance(country, str) or not country.strip()):
        raise PolicySnapshotError("country must be null or a non-empty string")
    # The derived list is derived: a persisted projection that disagrees with
    # its own sources is not a snapshot of anything, and stage zero would
    # judge rendered assertions against a fabricated allowed set.
    expected = build_work_authorization_projection({
        "authorized_in_country": projection["authorized_in_country"],
        "needs_sponsorship": projection["needs_sponsorship"],
        "country": country})["allowed_forms"]
    if projection["allowed_forms"] != expected:
        raise PolicySnapshotError(
            "allowed_forms does not match the projection recomputed from its"
            f" own source values: {projection['allowed_forms']} vs {expected}")
    if not isinstance(snapshot["never_render"], list) or any(
            not isinstance(s, str) for s in snapshot["never_render"]):
        raise PolicySnapshotError("never_render must be a list of strings")
    return snapshot


# -- a context view over the persisted snapshot -------------------------------

class _SnapshotSelection:
    """facts_for_capability over the persisted selection JSON, the one method
    the shipped verifier consumes from a SelectionReport."""

    def __init__(self, selection: dict):
        self._facts: dict[str, frozenset[str]] = {}
        for entry in selection.get("capabilities", []):
            self._facts[entry["capability_id"]] = frozenset(
                f["fact_id"] for chain in entry.get("chains", [])
                for f in chain.get("facts", []))

    def facts_for_capability(self, capability_id: str) -> frozenset[str]:
        return self._facts.get(capability_id, frozenset())


class _SnapshotStrategy:
    def __init__(self, strategy_version: int):
        self.strategy_version = strategy_version


class SnapshotContext:
    """A duck-typed GenerationContext over a persisted context snapshot dict:
    exactly the surface GroundingVerifier reads (renderable grounding view,
    role_family_id, strategy_version, selection), so a stored version can be
    re-verified against the same bytes generation saw."""

    def __init__(self, snapshot: dict):
        self._view = snapshot["renderable_grounding_view"]
        self.role_family_id = snapshot["role_family_id"]
        self.strategy = _SnapshotStrategy(snapshot["strategy"]["strategy_version"])
        self.selection = _SnapshotSelection(snapshot.get("selection", {}))

    def renderable_grounding_view(self) -> dict:
        return self._view


# -- stage zero ---------------------------------------------------------------

def run_invariants(*, cv: CvModel, snapshot: dict, snapshot_bytes: bytes,
                   input_context_hash: str, artifact_bytes: bytes,
                   artifact_hash: str, verifier_report_json: str | None,
                   ats_report_json: str | None, extracted_text: str,
                   policy_snapshot: dict) -> tuple[InvariantResult, ...]:
    """The six stage-zero rules, in order. A failed audit-integrity check
    returns alone: a package whose audit bundle does not verify is judged by
    nobody, and the remaining rules would run over unattested bytes."""
    audit = _check_audit_integrity(snapshot, snapshot_bytes, input_context_hash,
                                   artifact_bytes, artifact_hash,
                                   verifier_report_json, ats_report_json)
    if audit.disposition == FAIL:
        return (audit,)
    verifier_final = json.loads(verifier_report_json)["final"]
    ats_report = json.loads(ats_report_json)
    return (
        audit,
        _check_regrounding(cv, snapshot, verifier_final),
        _check_date_coherence(cv, snapshot, extracted_text),
        _check_work_authorization(extracted_text, policy_snapshot),
        _check_user_constraints(extracted_text, policy_snapshot),
        _check_artifact_recheck(cv, extracted_text, ats_report),
    )


def _check_snapshot_shape(snapshot) -> list[str]:
    """Structural validation of the persisted context snapshot before any
    rule dereferences it: a malformed persisted shape is an audit-integrity
    failure, never an exception."""
    if not isinstance(snapshot, dict):
        return ["context snapshot is not a JSON object"]
    problems = []
    view = snapshot.get("renderable_grounding_view")
    if not isinstance(view, dict):
        problems.append("renderable_grounding_view is not an object")
    else:
        for key in ("facts", "experiences", "profile", "capabilities"):
            if not isinstance(view.get(key), dict):
                problems.append(f"renderable_grounding_view.{key} is not an object")
        # Nested map value shapes: every row the rules dereference must be a
        # mapping with its required keys of the right type.
        if isinstance(view.get("experiences"), dict):
            for exp_id, row in view["experiences"].items():
                if not isinstance(row, dict) or not {
                        "kind", "title", "org", "start_date",
                        "end_date"} <= set(row):
                    problems.append(
                        f"experience row '{exp_id}' is not a mapping with the"
                        " required keys")
        if isinstance(view.get("facts"), dict):
            for fact_id, row in view["facts"].items():
                if not isinstance(row, dict) \
                        or not isinstance(row.get("statement"), str) \
                        or "experience_id" not in row:
                    problems.append(
                        f"fact row '{fact_id}' is not a mapping with statement"
                        " and experience_id")
        if isinstance(view.get("profile"), dict):
            for field, value in view["profile"].items():
                if value is not None and not isinstance(value, str):
                    problems.append(f"profile field '{field}' is not a string or null")
        if isinstance(view.get("capabilities"), dict):
            for cap_id, row in view["capabilities"].items():
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    problems.append(
                        f"capability row '{cap_id}' is not a mapping with a name")
    strategy = snapshot.get("strategy")
    if not isinstance(strategy, dict) or not isinstance(
            strategy.get("strategy_version"), int):
        problems.append("strategy block is malformed")
    selection = snapshot.get("selection")
    if not isinstance(selection, dict):
        problems.append("selection is not an object")
    else:
        # Every selection shape SnapshotContext and the verifier read:
        # capability entries as mappings with a capability_id, chains as
        # mappings, chain facts as mappings with a fact_id.
        capabilities = selection.get("capabilities", [])
        if not isinstance(capabilities, list):
            problems.append("selection.capabilities is not a list")
            capabilities = []
        for i, entry in enumerate(capabilities):
            if not isinstance(entry, dict) \
                    or not isinstance(entry.get("capability_id"), str):
                problems.append(
                    f"selection capability entry {i} is not a mapping with a"
                    " capability_id")
                continue
            chains = entry.get("chains", [])
            if not isinstance(chains, list):
                problems.append(f"selection capability entry {i} chains is not a list")
                continue
            for k, chain in enumerate(chains):
                if not isinstance(chain, dict) \
                        or not isinstance(chain.get("facts", []), list):
                    problems.append(
                        f"selection chain {i}.{k} is not a mapping with a"
                        " facts list")
                    continue
                for f, fact in enumerate(chain.get("facts", [])):
                    if not isinstance(fact, dict) \
                            or not isinstance(fact.get("fact_id"), str):
                        problems.append(
                            f"selection chain fact {i}.{k}.{f} is not a"
                            " mapping with a fact_id")
    if not isinstance(snapshot.get("role_family_id"), str):
        problems.append("role_family_id is not a string")
    return problems


def _check_audit_integrity(snapshot, snapshot_bytes: bytes, input_context_hash: str,
                           artifact_bytes: bytes, artifact_hash: str,
                           verifier_report_json: str | None,
                           ats_report_json: str | None) -> InvariantResult:
    problems = _check_snapshot_shape(snapshot)
    if hashlib.sha256(snapshot_bytes).hexdigest() != input_context_hash:
        problems.append("context snapshot bytes do not re-hash to input_context_hash")
    if hashlib.sha256(artifact_bytes).hexdigest() != artifact_hash:
        problems.append("stored artifact bytes do not re-hash to artifact_hash")
    final = None
    if not verifier_report_json:
        problems.append("verifier report is absent")
    else:
        try:
            trail = json.loads(verifier_report_json)
            final = trail["final"] if isinstance(trail, dict) else None
            if not isinstance(final, dict):
                final = None
                problems.append("verifier report trail has the wrong shape")
        except (json.JSONDecodeError, KeyError, TypeError):
            problems.append("verifier report is not a parseable trail")
    if final is not None:
        if not final.get("passed"):
            problems.append("stored verifier report did not pass")
        if final.get("spec_version") not in RECOGNIZED_VERIFIER_SPEC_VERSIONS:
            problems.append(
                f"verifier spec_version '{final.get('spec_version')}' is not a"
                " spec identifier this Gauntlet suite recognizes")
    if not ats_report_json:
        problems.append("ATS report is absent")
    else:
        try:
            ats = json.loads(ats_report_json)
            if not isinstance(ats, dict):
                problems.append("ATS report has the wrong shape")
            elif not ats.get("passed"):
                problems.append("stored ATS report did not pass")
        except json.JSONDecodeError:
            problems.append("ATS report is not parseable JSON")
    if problems:
        return InvariantResult("audit-integrity", FAIL, "; ".join(problems))
    return InvariantResult("audit-integrity", PASS, "audit bundle verifies")


def _check_regrounding(cv: CvModel, snapshot: dict, verifier_final: dict) -> InvariantResult:
    stored_spec = verifier_final.get("spec_version")
    snapshot_spec = snapshot.get("normalization_spec_version")
    if stored_spec != SPEC_VERSION or snapshot_spec != SPEC_VERSION:
        # Never PASS and never a silent skip: re-running an old snapshot under
        # new normalization semantics would judge different bytes than
        # generation saw.
        return InvariantResult(
            "regrounding", ATTENTION,
            f"regrounding-unsupported: stored spec {stored_spec!r} / snapshot spec"
            f" {snapshot_spec!r} vs shipped {SPEC_VERSION!r}")
    report = GroundingVerifier(SnapshotContext(snapshot)).verify(cv)
    if report.passed:
        return InvariantResult("regrounding", PASS, "grounding re-verifies")
    named = "; ".join(f"[{f.rule}] {f.element}: {f.message}"
                      for f in report.findings[:10])
    return InvariantResult("regrounding", FAIL, f"re-verification failed: {named}")


def _check_date_coherence(cv: CvModel, snapshot: dict, extracted_text: str) -> InvariantResult:
    """Coherence is judged on the canonical time value behind each date label
    (domain/dates.py), never on the label's characters: the stored label is
    what the CV displays, and "September 2015" is the same month as "2015-09".
    A label the parser cannot read is reported as unreadable (disposition
    `attention`), never asserted to be a contradiction."""
    problems: list[str] = []
    unreadable: list[str] = []
    for entry in cv.all_entries():
        for field, label in (("start_date", entry.start_date),
                             ("end_date", entry.end_date)):
            if label and not is_readable(label):
                unreadable.append(f"[{entry.experience_id}] {field} '{label}'")
        if starts_before_end(entry.start_date, entry.end_date) is False:
            problems.append(
                f"[{entry.experience_id}] start {entry.start_date} follows"
                f" end {entry.end_date}")
    # Reverse-chronological experience order against the canonical rows.
    rows = snapshot["renderable_grounding_view"]["experiences"]
    ordered = [row for entry in cv.experiences
               for row in [rows.get(entry.experience_id)] if row is not None]
    keys = [chron_rank(row["start_date"], row["end_date"]) for row in ordered]
    unordered = keys != sorted(keys, reverse=True)
    order_unreadable = any(not is_readable(row[field]) for row in ordered
                           for field in ("start_date", "end_date"))
    if unordered and not order_unreadable:
        problems.append("experience section is not reverse-chronological against"
                        " the canonical rows")
    elif unordered:
        unreadable.append("experience order could not be checked: a canonical"
                          " row carries an unreadable date")
    # No date in generated text postdates generated_at.
    horizon = parse(cv.meta.generated_at[:7])  # YYYY-MM
    for match in _RENDERED_DATE.finditer(normalize_chars(extracted_text)):
        name, name_year, iso_year, iso_month = match.groups()
        rendered = parse(f"{name} {name_year}" if name else
                         (f"{iso_year}-{iso_month}" if iso_month else iso_year))
        if rendered is None or horizon is None:
            continue
        if starts_before_end(rendered.iso, horizon.iso) is False:
            problems.append(
                f"rendered date {rendered.iso} postdates generated_at {horizon.iso}")
    if problems:
        return InvariantResult("date-coherence", FAIL, "; ".join(problems))
    if unreadable:
        return InvariantResult(
            "date-coherence", ATTENTION,
            "dates cohere as far as they can be read; unreadable date labels"
            " (fix them with `open-career onboard`, e.g. '2015-09' or"
            f" 'September 2015'): {'; '.join(unreadable)}")
    return InvariantResult("date-coherence", PASS, "dates cohere")


def is_authorization_assertion(sentence: str) -> bool:
    """Is this sentence an employment-authorization claim? The detector is
    part of the projection version: changing it bumps
    WORK_AUTH_PROJECTION_VERSION and the suite version with it."""
    normalized = normalize(sentence)
    return bool(normalized) and any(p.search(normalized) for p in _AUTH_PATTERNS)


def _check_work_authorization(extracted_text: str, policy_snapshot: dict) -> InvariantResult:
    projection = policy_snapshot["work_authorization"]
    allowed = {normalize(form) for form in projection.get("allowed_forms", [])}
    problems = []
    for raw in _SENTENCE_SPLIT.split(extracted_text):
        sentence = normalize(raw)
        if not is_authorization_assertion(raw):
            continue
        if sentence not in allowed:
            reason = ("no underlying authorization field is set"
                      if not allowed else "it matches no allowed form")
            problems.append(f"authorization-class assertion '{raw.strip()}' fails: {reason}")
    if problems:
        return InvariantResult("work-authorization", FAIL, "; ".join(problems))
    return InvariantResult("work-authorization", PASS,
                           "no unauthorized authorization-class assertion")


def _check_user_constraints(extracted_text: str, policy_snapshot: dict) -> InvariantResult:
    rendered = normalize(extracted_text)
    hits = [literal for literal in policy_snapshot.get("never_render", [])
            if normalize(literal) and normalize(literal) in rendered]
    if hits:
        return InvariantResult(
            "user-constraints", FAIL,
            f"never_render strings appear in the rendered text: {hits}")
    return InvariantResult("user-constraints", PASS, "never_render list is respected")


def _check_artifact_recheck(cv: CvModel, extracted_text: str, ats_report: dict) -> InvariantResult:
    """Page budget and section order re-asserted from the stored artifact via
    the same pdftotext path (defense in depth against a stale or hand-swapped
    artifact). The recorded page count is the budget: the stored artifact must
    still extract to exactly what was verified."""
    budget = max(1, int(ats_report.get("page_count", 1)))
    report = check_ats(extracted_text, cv, page_budget=budget)
    if report.passed:
        return InvariantResult("artifact-recheck", PASS,
                               "page budget and section order re-verify")
    named = "; ".join(f"[{f.check}] {f.message}" for f in report.findings[:5])
    return InvariantResult("artifact-recheck", FAIL, named)
