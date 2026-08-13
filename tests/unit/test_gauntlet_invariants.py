"""Stage-zero invariant tests (spec: the scope's decisions/gauntlet-design.md,
"Stage zero"): one test set per named rule, plus the policy snapshot
serialization contract. Also exports the shared synthetic case builders the
other Gauntlet test modules reuse."""

import hashlib
import json

from domain.ats_check import check_ats
from domain.cv_model import Bullet, CvExperienceEntry, CvHeader, CvMeta, CvModel, SkillItem
from domain.cv_sections import cv_sections
from domain.gauntlet_invariants import (
    ATTENTION,
    FAIL,
    PASS,
    SnapshotContext,
    build_policy_snapshot,
    build_work_authorization_projection,
    invariants_passed,
    policy_snapshot_json,
    run_invariants,
)
from domain.grounding import GroundingVerifier
from domain.grounding_spec import SPEC_VERSION

GENERATED_AT = "2026-08-12T00:00:00Z"

PROFILE = {
    "full_name": "Maya Lindqvist", "email": "maya@example.com",
    "phone": "+39 333 1234", "location": "Milan, Italy", "country": "Italy",
    "authorized_in_country": "yes", "needs_sponsorship": "no",
}

FACT = ("Reduced onboarding time by 40% for 12 enterprise customers by"
        " automating the Python deployment pipeline")


def make_cv(bullet_text: str = FACT, summary: str = "") -> CvModel:
    return CvModel(
        header=CvHeader(name=PROFILE["full_name"], email=PROFILE["email"],
                        phone=PROFILE["phone"], location=PROFILE["location"]),
        summary=summary,
        skills=(SkillItem(name="Python", capability_ids=("cap_1",)),),
        experiences=(CvExperienceEntry(
            experience_id="exp_1", title="Forward Deployed Engineer", org="Acme",
            start_date="2022-03", end_date="2024-05",
            bullets=(Bullet(text=bullet_text, fact_ids=("fact_1",)),)),),
        meta=CvMeta(role_family_id="rf_1", strategy_version=1,
                    generated_at=GENERATED_AT))


def make_snapshot() -> dict:
    return {
        "normalization_spec_version": SPEC_VERSION,
        "role_family_id": "rf_1",
        "selection": {
            "capabilities": [{"capability_id": "cap_1", "covered": True,
                              "chains": [{"facts": [{"fact_id": "fact_1"}]}]}],
            "gaps": [],
        },
        "renderable_grounding_view": {
            "facts": {"fact_1": {"statement": FACT, "fact_type": "achievement",
                                 "experience_id": "exp_1"}},
            "experiences": {"exp_1": {
                "kind": "role", "title": "Forward Deployed Engineer",
                "org": "Acme", "start_date": "2022-03", "end_date": "2024-05"}},
            "profile": dict(PROFILE),
            "capabilities": {"cap_1": {"name": "Python", "strength": "strong"}},
        },
        "strategy": {"strategy_version": 1, "objective": "o", "allocation": 5,
                     "family": {"id": "rf_1", "name": "FDE"}},
    }


def extracted_text(cv: CvModel) -> str:
    """The section token stream the real renderer would extract, synthesized
    deterministically from the same sectioning the ATS check uses."""
    lines = []
    for section in cv_sections(cv):
        if section.heading:
            lines.append(section.heading)
        for block in section.blocks:
            lines.extend(block.lines)
    return "\n".join(lines)


def make_case(cv=None, snapshot=None, policies=None, profile=None,
              text=None):
    """A complete consistent audit bundle for stage zero, tamperable per
    test. Returns kwargs for run_invariants."""
    cv = cv or make_cv()
    snapshot = snapshot or make_snapshot()
    snapshot_bytes = json.dumps(snapshot, indent=2, sort_keys=True).encode()
    artifact_bytes = b"%PDF-1.4 synthetic"
    text = text if text is not None else extracted_text(cv)
    verifier_report = GroundingVerifier(SnapshotContext(snapshot)).verify(cv)
    trail = json.dumps({"final": json.loads(verifier_report.to_json()),
                        "attempts": 1, "fallback_used": False, "dropped": []})
    ats = check_ats(text, cv, 1).to_json()
    return dict(
        cv=cv, snapshot=snapshot, snapshot_bytes=snapshot_bytes,
        input_context_hash=hashlib.sha256(snapshot_bytes).hexdigest(),
        artifact_bytes=artifact_bytes,
        artifact_hash=hashlib.sha256(artifact_bytes).hexdigest(),
        verifier_report_json=trail, ats_report_json=ats,
        extracted_text=text,
        policy_snapshot=build_policy_snapshot(profile or PROFILE, policies or {}))


def _by_rule(results):
    return {r.rule: r for r in results}


# -- the clean case -----------------------------------------------------------

def test_clean_case_passes_every_rule():
    results = run_invariants(**make_case())
    assert invariants_passed(results)
    assert [r.disposition for r in results] == [PASS] * 6


# -- audit-integrity ----------------------------------------------------------

def test_tampered_snapshot_bytes_fail_audit_and_stop_stage_zero():
    case = make_case()
    case["snapshot_bytes"] = case["snapshot_bytes"] + b" "
    results = run_invariants(**case)
    assert len(results) == 1  # judged by nobody: remaining rules never ran
    assert results[0].rule == "audit-integrity" and results[0].disposition == FAIL


def test_tampered_artifact_hash_fails_audit():
    case = make_case()
    case["artifact_hash"] = "0" * 64
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "artifact" in result.detail


def test_absent_or_failed_reports_fail_audit():
    case = make_case()
    case["verifier_report_json"] = None
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "verifier report is absent" in result.detail
    case = make_case()
    failed = json.loads(case["ats_report_json"])
    failed["passed"] = False
    case["ats_report_json"] = json.dumps(failed)
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "ATS report did not pass" in result.detail


def test_unknown_verifier_spec_version_fails_audit():
    case = make_case()
    trail = json.loads(case["verifier_report_json"])
    trail["final"]["spec_version"] = "0"
    case["verifier_report_json"] = json.dumps(trail)
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "spec_version" in result.detail


def test_malformed_report_objects_fail_audit_never_raise():
    # Wrong-type JSON: the trail is an array where an object is expected.
    case = make_case()
    case["verifier_report_json"] = "[1, 2, 3]"
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "wrong shape" in result.detail
    # The final block is an array.
    case = make_case()
    case["verifier_report_json"] = json.dumps({"final": [1]})
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "wrong shape" in result.detail
    # The ATS report is a bare string.
    case = make_case()
    case["ats_report_json"] = '"looks fine"'
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "ATS report has the wrong shape" in result.detail


def test_malformed_context_snapshot_fails_audit_never_raises():
    # The renderable view is an array; nothing downstream dereferences it.
    snapshot = make_snapshot()
    snapshot["renderable_grounding_view"] = ["not", "an", "object"]
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL
    assert "renderable_grounding_view is not an object" in result.detail
    # The whole snapshot is a scalar.
    case = make_case()
    case["snapshot"] = "garbage"
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL
    assert "not a JSON object" in result.detail


def test_malformed_nested_snapshot_values_fail_audit_never_raise():
    # An experience row that is an array.
    snapshot = make_snapshot()
    snapshot["renderable_grounding_view"]["experiences"]["exp_1"] = ["role"]
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "experience row 'exp_1'" in result.detail
    # A fact row that is a scalar.
    snapshot = make_snapshot()
    snapshot["renderable_grounding_view"]["facts"]["fact_1"] = "just a string"
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "fact row 'fact_1'" in result.detail
    # A profile field that is an object; a capability row missing its name.
    snapshot = make_snapshot()
    snapshot["renderable_grounding_view"]["profile"]["location"] = {"city": "x"}
    snapshot["renderable_grounding_view"]["capabilities"]["cap_1"] = 7
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL
    assert "profile field 'location'" in result.detail
    assert "capability row 'cap_1'" in result.detail


def test_malformed_selection_shapes_fail_audit_never_raise():
    # A capability entry that is an array.
    snapshot = make_snapshot()
    snapshot["selection"]["capabilities"][0] = ["cap_1"]
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL
    assert "capability entry 0 is not a mapping" in result.detail
    # A chain that is a scalar.
    snapshot = make_snapshot()
    snapshot["selection"]["capabilities"][0]["chains"] = ["not a chain"]
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL and "chain 0.0 is not a mapping" in result.detail
    # A chain fact missing its fact_id.
    snapshot = make_snapshot()
    snapshot["selection"]["capabilities"][0]["chains"][0]["facts"] = [{"proof": "x"}]
    case = make_case()
    case["snapshot"] = snapshot
    (result,) = run_invariants(**case)
    assert result.disposition == FAIL
    assert "chain fact 0.0.0 is not a mapping with a fact_id" in result.detail


def test_policy_snapshot_parse_is_fail_closed():
    import pytest

    from domain.gauntlet_invariants import PolicySnapshotError, parse_policy_snapshot

    good = policy_snapshot_json(build_policy_snapshot(PROFILE, {})).encode()
    assert parse_policy_snapshot(good)["never_render"] == []
    for bad in (b"not json", b"[1]", b'{"never_render": []}',
                b'{"work_authorization": [], "never_render": []}',
                json.dumps({"work_authorization": {
                    "projection_version": "wa-999", "authorized_in_country": None,
                    "needs_sponsorship": None, "country": None,
                    "allowed_forms": []}, "never_render": []}).encode(),
                json.dumps({"work_authorization": json.loads(good)["work_authorization"],
                            "never_render": [1]}).encode()):
        with pytest.raises(PolicySnapshotError):
            parse_policy_snapshot(bad)


def test_policy_snapshot_source_values_are_the_closed_vocabulary():
    import pytest

    from domain.gauntlet_invariants import PolicySnapshotError, parse_policy_snapshot

    def snapshot_with(**overrides):
        projection = build_work_authorization_projection(PROFILE)
        projection.update(overrides)
        return json.dumps({"work_authorization": projection,
                           "never_render": []}).encode()

    for bad, message in (
            ({"authorized_in_country": "true"}, "authorized_in_country"),
            ({"authorized_in_country": 1}, "authorized_in_country"),
            ({"needs_sponsorship": "maybe"}, "needs_sponsorship"),
            ({"country": ""}, "country"),
            ({"country": 42}, "country")):
        with pytest.raises(PolicySnapshotError, match=message):
            parse_policy_snapshot(snapshot_with(**bad))


def test_a_projection_disagreeing_with_its_own_sources_is_refused():
    """allowed_forms is DERIVED: a persisted projection that does not recompute
    from its own source values is not a snapshot of anything, and stage zero
    would judge rendered assertions against a fabricated allowed set."""
    import pytest

    from domain.gauntlet_invariants import PolicySnapshotError, parse_policy_snapshot

    projection = build_work_authorization_projection(PROFILE)
    projection["allowed_forms"] = projection["allowed_forms"] + ["requires sponsorship"]
    data = json.dumps({"work_authorization": projection,
                       "never_render": []}).encode()
    with pytest.raises(PolicySnapshotError, match="does not match the projection"):
        parse_policy_snapshot(data)
    # The source values changing without the derived list is refused too.
    projection = build_work_authorization_projection(PROFILE)
    projection["needs_sponsorship"] = "yes"
    with pytest.raises(PolicySnapshotError, match="does not match the projection"):
        parse_policy_snapshot(json.dumps(
            {"work_authorization": projection, "never_render": []}).encode())


# -- regrounding --------------------------------------------------------------

def test_regrounding_reruns_and_catches_an_ungrounded_swap():
    # The stored reports verify (built from the clean cv), then the content
    # model is swapped for one whose bullet is ungrounded: only the
    # re-run catches it.
    case = make_case()
    bad_cv = make_cv(bullet_text="Invented a quantum profit machine")
    case["cv"] = bad_cv
    case["extracted_text"] = extracted_text(bad_cv)
    # Keep ATS coherent with the swapped artifact text so the failure is
    # attributable to regrounding alone.
    case["ats_report_json"] = check_ats(case["extracted_text"], bad_cv, 1).to_json()
    results = run_invariants(**case)
    assert _by_rule(results)["regrounding"].disposition == FAIL


def test_spec_version_drift_is_attention_named_regrounding_unsupported():
    snapshot = make_snapshot()
    snapshot["normalization_spec_version"] = "1"
    case = make_case(snapshot=snapshot)
    result = _by_rule(run_invariants(**case))["regrounding"]
    assert result.disposition == ATTENTION
    assert "regrounding-unsupported" in result.detail
    # Attention is not a failure: the judges still run.
    assert invariants_passed(run_invariants(**case))


# -- date-coherence -----------------------------------------------------------

def test_start_after_end_fails_date_coherence():
    cv = make_cv()
    entry = cv.experiences[0]
    swapped = CvExperienceEntry(**{**entry.__dict__, "start_date": "2024-05",
                                   "end_date": "2022-03"})
    cv = CvModel(**{**cv.__dict__, "experiences": (swapped,)})
    case = make_case()
    case["cv"] = cv
    result = _by_rule(run_invariants(**case))["date-coherence"]
    assert result.disposition == FAIL and "follows end" in result.detail


def test_rendered_date_postdating_generated_at_fails():
    case = make_case(text=extracted_text(make_cv()) + "\nExpected through 2030-01")
    # Re-align the ATS report so only the date rule fires.
    case["ats_report_json"] = check_ats(extracted_text(make_cv()), make_cv(), 1).to_json()
    results = {r.rule: r for r in run_invariants(**case)}
    assert results["date-coherence"].disposition == FAIL
    assert "postdates" in results["date-coherence"].detail


def test_out_of_order_experience_section_fails():
    snapshot = make_snapshot()
    snapshot["renderable_grounding_view"]["experiences"]["exp_2"] = {
        "kind": "role", "title": "Engineer", "org": "Beta",
        "start_date": "2018-01", "end_date": "2020-01"}
    snapshot["renderable_grounding_view"]["facts"]["fact_2"] = {
        "statement": FACT, "fact_type": "achievement", "experience_id": "exp_2"}
    cv = make_cv()
    older = CvExperienceEntry(experience_id="exp_2", title="Engineer", org="Beta",
                              start_date="2018-01", end_date="2020-01",
                              bullets=(Bullet(text=FACT, fact_ids=("fact_2",)),))
    cv = CvModel(**{**cv.__dict__, "experiences": (older,) + cv.experiences})
    case = make_case(cv=cv, snapshot=snapshot)
    # The stored trail claims a pass (the stale-bundle class this rule
    # defends against); the canonical rows still expose the wrong order.
    case["verifier_report_json"] = json.dumps(
        {"final": {"passed": True, "spec_version": SPEC_VERSION, "findings": []},
         "attempts": 1, "fallback_used": False, "dropped": []})
    result = _by_rule(run_invariants(**case))["date-coherence"]
    assert result.disposition == FAIL and "reverse-chronological" in result.detail


# -- work-authorization -------------------------------------------------------

ORDINARY_CV_SENTENCES = (
    "Sponsored a community event for 200 developers",
    "Sponsored the internal guild for platform engineers",
    "Built tooling for the citizen developer program",
    "Led the sponsorship of a conference track",
    "Owned the sponsor portal and its billing integration",
    "Shipped the visa free travel comparison feature",
    "Ran the sponsor onboarding workshop",
)

AUTHORIZATION_SENTENCES = (
    # The frozen corpus's wrong-work-authorization phrasing.
    "Requires visa sponsorship to work in the Netherlands",
    "Authorized to work in Italy",
    "Legally authorized to work",
    "No visa sponsorship required",
    "Does not require sponsorship",
    "Holds a valid work permit",
    "Eligible to work in the EU",
    "Right to work in the United Kingdom",
    "Green card holder",
    "Permanent resident",
    "EU citizen with the right to work anywhere in the union",
)


def test_ordinary_grounded_cv_text_is_not_an_authorization_assertion():
    """A false blocking stage-zero failure rejects an honest package, which is
    worse here than a miss: the rule exists to stop a claim the user never
    made, not to police the word 'sponsor'."""
    from domain.gauntlet_invariants import is_authorization_assertion

    for sentence in ORDINARY_CV_SENTENCES:
        assert not is_authorization_assertion(sentence), sentence


def test_real_authorization_assertions_are_detected():
    from domain.gauntlet_invariants import is_authorization_assertion

    for sentence in AUTHORIZATION_SENTENCES:
        assert is_authorization_assertion(sentence), sentence


def test_ordinary_sponsorship_wording_passes_the_rule():
    """The whole rule, not only the detector: an ordinary bullet mentioning
    sponsorship or citizen developers never fails stage zero."""
    from domain.gauntlet_invariants import _check_work_authorization

    snapshot = build_policy_snapshot(PROFILE, {})
    for sentence in ORDINARY_CV_SENTENCES:
        result = _check_work_authorization(
            f"{extracted_text(make_cv())}\n{sentence}.", snapshot)
        assert result.disposition == PASS, (sentence, result.detail)
    # The real assertion still fails, since it matches no allowed form.
    failing = _check_work_authorization(
        "Requires visa sponsorship to work in the Netherlands.", snapshot)
    assert failing.disposition == FAIL


def test_each_allowed_form_matches_deterministically():
    projection = build_work_authorization_projection(PROFILE)
    assert projection["projection_version"] == "wa-2"
    for form in projection["allowed_forms"]:
        case = make_case(text=extracted_text(make_cv()) + f"\n{form}.")
        case["ats_report_json"] = check_ats(extracted_text(make_cv()),
                                            make_cv(), 1).to_json()
        result = _by_rule(run_invariants(**case))["work-authorization"]
        assert result.disposition == PASS, form


def test_wrong_authorization_assertion_fails_deterministically():
    case = make_case(text=extracted_text(make_cv()) + "\nUS citizen, no visa needed")
    case["ats_report_json"] = check_ats(extracted_text(make_cv()), make_cv(), 1).to_json()
    result = _by_rule(run_invariants(**case))["work-authorization"]
    assert result.disposition == FAIL and "matches no allowed form" in result.detail


def test_assertion_with_absent_underlying_field_fails():
    profile = {k: v for k, v in PROFILE.items()
               if k not in ("authorized_in_country", "needs_sponsorship")}
    case = make_case(profile=profile,
                     text=extracted_text(make_cv()) + "\nAuthorized to work")
    case["ats_report_json"] = check_ats(extracted_text(make_cv()), make_cv(), 1).to_json()
    result = _by_rule(run_invariants(**case))["work-authorization"]
    assert result.disposition == FAIL
    assert "no underlying authorization field" in result.detail


# -- user-constraints ---------------------------------------------------------

def test_never_render_hit_fails_and_normalized_form_is_caught():
    cv = make_cv()
    case = make_case(policies={"never_render": ["Family–Business Srl"]},
                     text=extracted_text(cv) + "\nWorked at family-business srl")
    case["ats_report_json"] = check_ats(extracted_text(cv), cv, 1).to_json()
    result = _by_rule(run_invariants(**case))["user-constraints"]
    assert result.disposition == FAIL


def test_never_render_clean_passes():
    case = make_case(policies={"never_render": ["Family Business Srl"]})
    assert _by_rule(run_invariants(**case))["user-constraints"].disposition == PASS


# -- artifact recheck ---------------------------------------------------------

def test_hand_swapped_artifact_text_fails_the_recheck():
    cv = make_cv()
    good_text = extracted_text(cv)
    case = make_case()
    # Reports and hashes agree, but the stored artifact now extracts to
    # something else entirely (the hand-swapped artifact class); only the
    # re-extraction path catches it.
    case["extracted_text"] = good_text.replace("40%", "80%")
    result = _by_rule(run_invariants(**case))["artifact-recheck"]
    assert result.disposition == FAIL


def test_page_overflow_fails_the_recheck():
    case = make_case()
    case["extracted_text"] = case["extracted_text"] + "\f\f"
    result = _by_rule(run_invariants(**case))["artifact-recheck"]
    assert result.disposition == FAIL


# -- the policy snapshot ------------------------------------------------------

def test_policy_snapshot_is_canonical_and_carries_exactly_the_consumed_inputs():
    snapshot = build_policy_snapshot(PROFILE, {"never_render": ["X"],
                                               "eeo_stance": "always_decline"})
    assert set(snapshot) == {"work_authorization", "never_render"}
    assert snapshot["never_render"] == ["X"]
    assert snapshot["work_authorization"]["authorized_in_country"] == "yes"
    # Canonical bytes: same inputs, same serialization, byte for byte.
    assert policy_snapshot_json(snapshot) == policy_snapshot_json(
        build_policy_snapshot(dict(PROFILE), {"never_render": ["X"]}))


def test_projection_copies_the_closed_strings_verbatim_and_derives_forms():
    projection = build_work_authorization_projection(
        {"authorized_in_country": "yes", "needs_sponsorship": "yes"})
    assert projection["needs_sponsorship"] == "yes"
    assert "requires sponsorship" in projection["allowed_forms"]
    assert "no sponsorship required" not in projection["allowed_forms"]
    empty = build_work_authorization_projection({})
    assert empty["allowed_forms"] == []


# -- the drive regression: human date labels ----------------------------------

# The driver's real four-role CV, verbatim. Two of these roles invert
# alphabetically (September > July, June > February), which is exactly what a
# raw string comparison reported as "start follows end": no CV written the way
# people write CVs could clear stage zero.
DRIVER_ROLES = (
    ("exp_1", "Support Engineer", "Alpha", "September 2015", "July 2017"),
    ("exp_2", "Engineer", "Beta", "August 2017", "May 2019"),
    ("exp_3", "Senior Engineer", "Gamma", "June 2019", "February 2022"),
    ("exp_4", "Forward Deployed Engineer", "Acme", "March 2022", None),
)


def _driver_case(order=None):
    """The driver's CV as a full audit bundle, entries in reverse-chronological
    order (open-ended role first) unless `order` says otherwise."""
    roles = {r[0]: r for r in DRIVER_ROLES}
    ids = order or [r[0] for r in reversed(DRIVER_ROLES)]
    snapshot = make_snapshot()
    view = snapshot["renderable_grounding_view"]
    view["experiences"] = {}
    view["facts"] = {}
    entries = []
    for n, exp_id in enumerate(ids, start=1):
        _, title, org, start, end = roles[exp_id]
        fact_id = f"fact_{exp_id}"
        view["experiences"][exp_id] = {"kind": "role", "title": title, "org": org,
                                       "start_date": start, "end_date": end}
        view["facts"][fact_id] = {"statement": FACT, "fact_type": "achievement",
                                  "experience_id": exp_id}
        entries.append(CvExperienceEntry(
            experience_id=exp_id, title=title, org=org, start_date=start,
            end_date=end, bullets=(Bullet(text=FACT, fact_ids=(fact_id,)),)))
    snapshot["selection"]["capabilities"] = [{
        "capability_id": "cap_1", "covered": True,
        "chains": [{"facts": [{"fact_id": f"fact_{i}"} for i in ids]}]}]
    cv = make_cv()
    cv = CvModel(**{**cv.__dict__, "experiences": tuple(entries)})
    return make_case(cv=cv, snapshot=snapshot)


def test_human_month_year_dates_cohere_and_order():
    """The drive's blocker: 'September 2015' to 'July 2017' is a coherent role
    and the current role leads the section."""
    result = _by_rule(run_invariants(**_driver_case()))["date-coherence"]
    assert result.disposition == PASS, result.detail


def test_human_dates_out_of_order_still_fail():
    """The rule did not stop working: the same labels in the wrong order (a
    2015 role rendered above the current one) are still caught."""
    case = _driver_case(order=["exp_1", "exp_4", "exp_3", "exp_2"])
    # The stored trail claims a pass (the stale-bundle class), so stage zero
    # reaches the date rule instead of stopping at audit-integrity.
    case["verifier_report_json"] = json.dumps(
        {"final": {"passed": True, "spec_version": SPEC_VERSION, "findings": []},
         "attempts": 1, "fallback_used": False, "dropped": []})
    result = _by_rule(run_invariants(**case))["date-coherence"]
    assert result.disposition == FAIL and "reverse-chronological" in result.detail


def test_a_month_name_date_postdating_generated_at_is_caught():
    """The horizon check reads month-name dates too: it used to see only the
    bare year in 'January 2030'."""
    case = _driver_case()
    case["extracted_text"] = case["extracted_text"] + "\nExpected through January 2030"
    result = _by_rule(run_invariants(**case))["date-coherence"]
    assert result.disposition == FAIL and "2030-01" in result.detail


def test_an_unreadable_date_is_attention_never_a_false_contradiction():
    """A label the parser cannot read is reported as unreadable, not asserted
    to be a contradiction: the run continues to the judges, capped."""
    case = _driver_case()
    entry = case["cv"].experiences[0]
    broken = CvExperienceEntry(**{**entry.__dict__, "start_date": "mid-2022"})
    case["cv"] = CvModel(**{**case["cv"].__dict__,
                            "experiences": (broken,) + case["cv"].experiences[1:]})
    case["snapshot"]["renderable_grounding_view"]["experiences"][
        entry.experience_id]["start_date"] = "mid-2022"
    result = _by_rule(run_invariants(**case))["date-coherence"]
    assert result.disposition == ATTENTION
    assert "mid-2022" in result.detail
