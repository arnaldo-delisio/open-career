"""Deterministic grounding verifier tests (spec:
decisions/package-generation-design.md, "The output gate"). Implementer-side
unit tests; the named regression list also gets a separate discrimination-
evidenced pass per the design's verification plan."""

import pytest

from domain.context import GenerationContext, StrategySnapshot
from domain.cv_model import (
    Bullet,
    CvExperienceEntry,
    CvHeader,
    CvMeta,
    CvModel,
    SkillItem,
)
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, Evidence, Experience, RoleFamily
from domain.grounding import GroundingVerifier
from domain.grounding_spec import SPEC_VERSION, numeric_tokens, tokenize
from domain.selection import CapabilitySelection, SelectionReport
from domain.traversal import EvidenceChain, FactChain

EXP = Experience(id="exp_1", kind="role", title="Forward Deployed Engineer",
                 org="Acme", start_date="2022-03", end_date="2024-05")
EXP2 = Experience(id="exp_2", kind="role", title="Analyst", org="OtherCorp",
                  start_date="2019-01", end_date="2021-12")
FACT = CareerFact(id="fact_1", fact_type="achievement",
                  statement="Reduced onboarding time by 40% for 12 enterprise customers"
                            " by automating the Python deployment pipeline",
                  source="interview", user_approved=1, experience_id="exp_1")
FACT2 = CareerFact(id="fact_2", fact_type="responsibility",
                   statement="Contributed to the internal analytics dashboard",
                   source="interview", user_approved=1, experience_id="exp_2")
CAP = Capability(id="cap_1", name="Python", strength="strong")
FAMILY = RoleFamily(id="rf_1", name="FDE", rationale="rationale",
                    adjacent_titles=("Solutions Engineer",),
                    search_vocabulary=("forward deployed",))
PROFILE = {"full_name": "Test Person", "email": "t@example.com",
           "phone": "+39 333 1234567", "location": "Milan, Italy",
           "github_url": "https://github.com/example"}


def _edge(eid, et, src, tgt):
    return CareerEdge(id=eid, source_type=src[0], source_id=src[1], edge_type=et,
                      target_type=tgt[0], target_id=tgt[1], claim_kind="fact",
                      provenance="test", created_by="user", user_verified=1)


def make_context(facts=(FACT, FACT2), experiences=(EXP, EXP2),
                 objective="Land a staff FDE role at a hot AI startup called SecretCo"):
    evidence = Evidence(id="ev_1", evidence_type="cv", title="cv")
    chains = tuple(
        FactChain(proves_edge=_edge(f"edge_p{i}", "PROVES", ("evidence", "ev_1"),
                                    ("career_fact", f.id)),
                  fact=f,
                  experience=next((e for e in experiences if e.id == f.experience_id), None))
        for i, f in enumerate(facts))
    selection = SelectionReport(role_family_id="rf_1", selections=(
        CapabilitySelection(capability=CAP, chains=(
            EvidenceChain(supports_edge=_edge("edge_s", "SUPPORTS", ("evidence", "ev_1"),
                                              ("capability", "cap_1")),
                          evidence=evidence, facts=chains),)),))
    return GenerationContext(
        role_family_id="rf_1", selection=selection, profile=dict(PROFILE),
        experiences=tuple(experiences),
        strategy=StrategySnapshot(family=FAMILY, strategy_version=1,
                                  objective=objective, allocation=5))


def make_cv(summary="", skills=(SkillItem(name="Python", capability_ids=("cap_1",)),),
            bullets=(Bullet(text="Reduced onboarding time by 40% for 12 enterprise"
                                 " customers by automating the Python deployment pipeline",
                            fact_ids=("fact_1",)),),
            entry_overrides=None, header=None):
    entry = CvExperienceEntry(
        experience_id="exp_1", title=EXP.title, org=EXP.org,
        start_date=EXP.start_date, end_date=EXP.end_date, bullets=tuple(bullets))
    if entry_overrides:
        entry = CvExperienceEntry(**{**entry.__dict__, **entry_overrides})
    return CvModel(
        header=header or CvHeader(name="Test Person", email="t@example.com",
                                  phone="+39 333 1234567", location="Milan, Italy",
                                  links=("https://github.com/example",)),
        summary=summary, skills=tuple(skills), experiences=(entry,),
        meta=CvMeta(role_family_id="rf_1", strategy_version=1,
                    generated_at="2026-08-11T00:00:00Z"))


def verify(cv, context=None):
    return GroundingVerifier(context or make_context()).verify(cv)


def rules(report):
    return {f.rule for f in report.findings}


def test_grounded_cv_passes():
    report = verify(make_cv())
    assert report.passed, report.to_json()
    assert report.spec_version == SPEC_VERSION


def test_invented_number_fails():
    report = verify(make_cv(bullets=(Bullet(
        text="Reduced onboarding time by 60% by automating the Python deployment pipeline",
        fact_ids=("fact_1",)),)))
    assert "numbers-dates" in rules(report)


def test_number_word_form_is_tolerated():
    assert numeric_tokens("forty %") == numeric_tokens("40%")


def test_number_with_wrong_unit_fails():
    report = verify(make_cv(bullets=(Bullet(
        text="Reduced onboarding time by 40x by automating the Python deployment pipeline",
        fact_ids=("fact_1",)),)))
    assert "numbers-dates" in rules(report)


def test_unknown_entity_fails_closed():
    report = verify(make_cv(bullets=(Bullet(
        text="Automating the Python deployment pipeline with Kubernetes",
        fact_ids=("fact_1",)),)))
    assert "entities" in rules(report)


def test_unsupported_content_word_fails():
    report = verify(make_cv(bullets=(Bullet(
        text="Improved reliability of the Python deployment pipeline",
        fact_ids=("fact_1",)),)))
    assert "content-words" in rules(report)


def test_scope_inflation_contributed_never_becomes_led():
    """The prior repos' VERIFY_SOUL incident as a named rule: 'contributed to
    X' may never render as 'led X'."""
    context = make_context()
    cv = make_cv(bullets=(Bullet(text="Led the internal analytics dashboard",
                                 fact_ids=("fact_2",)),),
                 entry_overrides=dict(experience_id="exp_2", title=EXP2.title,
                                      org=EXP2.org, start_date=EXP2.start_date,
                                      end_date=EXP2.end_date))
    report = verify(cv, context)
    assert "scope-inflation" in rules(report)


def test_used_never_renders_as_built():
    """career-ops tool-of-trade conflation: 'used X' never becomes 'built X'."""
    fact = CareerFact(id="fact_u", fact_type="skill_use",
                      statement="Used the Terraform provisioning stack daily",
                      source="interview", user_approved=1, experience_id="exp_1")
    context = make_context(facts=(fact,))
    cv = make_cv(bullets=(Bullet(text="Built the Terraform provisioning stack",
                                 fact_ids=("fact_u",)),))
    report = verify(cv, context)
    assert "scope-inflation" in rules(report)


def test_cross_experience_misattribution_fails():
    """A reachable fact from one employer can never render under another."""
    report = verify(make_cv(bullets=(Bullet(
        text="Contributed to the internal analytics dashboard",
        fact_ids=("fact_2",)),)))
    assert "traceability" in rules(report)


def test_unreachable_fact_id_fails():
    report = verify(make_cv(bullets=(Bullet(text="Whatever", fact_ids=("fact_ghost",)),)))
    assert "traceability" in rules(report)


def test_experience_with_no_bullets_fails_gate():
    report = verify(make_cv(bullets=()))
    assert "experience-gate" in rules(report)


def test_skeleton_fields_must_match_canonical_row():
    report = verify(make_cv(entry_overrides=dict(title="VP of Engineering")))
    assert "skeleton" in rules(report)
    report = verify(make_cv(entry_overrides=dict(end_date="2025-01")))
    assert "skeleton" in rules(report)


def test_canonical_dates_need_no_repeating_fact():
    """Skeleton dates check against the confirmed row, not the facts."""
    report = verify(make_cv())
    assert not [f for f in report.findings if f.rule == "skeleton"]


def test_uncovered_capability_never_renders_as_skill():
    report = verify(make_cv(skills=(SkillItem(name="Rust", capability_ids=("cap_rust",)),)))
    assert "skills" in rules(report)


def test_skill_display_name_must_be_canonical():
    report = verify(make_cv(skills=(SkillItem(name="Snakes", capability_ids=("cap_1",)),)))
    assert "skills" in rules(report)


def test_summary_checked_against_renderable_view():
    ok = verify(make_cv(summary="Forward Deployed Engineer automating Python"
                                " deployment pipelines for enterprise customers."))
    assert ok.passed, ok.to_json()
    bad = verify(make_cv(summary="Visionary blockchain evangelist."))
    assert "content-words" in rules(bad)


def test_poisoned_strategy_cannot_leak_into_summary():
    """Strategy control data is non-renderable: its words never enter the
    grounding allowlist (per summary check class)."""
    context = make_context(objective="Join SecretCo as staff visionary")
    entity = verify(make_cv(summary="Targeting SecretCo."), context)
    assert "entities" in rules(entity)
    content = verify(make_cv(summary="A visionary engineer."), context)
    assert "content-words" in rules(content)
    number = verify(make_cv(
        summary="Automating Python deployment pipelines since 2044."),
        make_context(objective="By 2044 run everything"))
    assert "numbers-dates" in rules(number)


def test_header_must_match_profile():
    report = verify(make_cv(header=CvHeader(name="Someone Else")))
    assert "header" in rules(report)
    report = verify(make_cv(header=CvHeader(name="Test Person",
                                            links=("https://evil.example",))))
    assert "header" in rules(report)


def test_urls_and_versions_do_not_split():
    assert "https://github.com/example" in tokenize("see https://github.com/example now")
    assert "3.12" in tokenize("Python 3.12")
