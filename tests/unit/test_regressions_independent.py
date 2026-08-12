"""INDEPENDENT regression pass (doctrine TDD rule): named regression tests
re-authored by a separate pass from the implementer, each with recorded
discrimination evidence (cp-backup, mutate, FAIL, byte-identical cp restore,
PASS). Spec: decisions/package-generation-design.md.

Fixtures here are authored fresh, deliberately not shared with the
implementer's test modules."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
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
from domain.generation import build_verbatim_model
from domain.grounding import GroundingVerifier
from domain.selection import CapabilitySelection, FamilyEvidenceSelection, SelectionReport
from domain.traversal import EvidenceChain, EvidenceTraversal, FactChain

# -- independent fixture world ----------------------------------------------

EXP_A = Experience(id="iexp_a", kind="role", title="Platform Engineer",
                   org="Nimbus Systems", start_date="2020-01", end_date="2023-06")
EXP_B = Experience(id="iexp_b", kind="role", title="Consultant",
                   org="Delta Partners", start_date="2018-02", end_date="2019-12")
FACT_A1 = CareerFact(
    id="ifact_a1", fact_type="achievement",
    statement="Reduced deployment failures by 35% across 8 services by"
              " introducing a Terraform release pipeline",
    source="interview", user_approved=1, experience_id="iexp_a")
FACT_A2 = CareerFact(
    id="ifact_a2", fact_type="skill_use",
    statement="Used Terraform daily to provision staging environments",
    source="interview", user_approved=1, experience_id="iexp_a")
FACT_B1 = CareerFact(
    id="ifact_b1", fact_type="responsibility",
    statement="Contributed to a churn prediction machine learning model used"
              " by the sales team",
    source="interview", user_approved=1, experience_id="iexp_b")
CAP_TF = Capability(id="icap_tf", name="Terraform", strength="strong")
CAP_ML = Capability(id="icap_ml", name="Machine Learning", strength="moderate")
FAMILY = RoleFamily(id="irf_1", name="Platform", rationale="test rationale",
                    adjacent_titles=("Infrastructure Engineer",),
                    search_vocabulary=("kubernetes", "platform"))
PROFILE = {"full_name": "Casey Sample", "email": "casey@sample.dev",
           "phone": "+1 555 0100", "location": "Lisbon, Portugal",
           "github_url": "https://github.com/casey-sample"}


def _edge(eid, edge_type, src, tgt, **kw):
    defaults = dict(claim_kind="fact", provenance="independent-test",
                    created_by="user", user_verified=1)
    defaults.update(kw)
    return CareerEdge(id=eid, source_type=src[0], source_id=src[1],
                      edge_type=edge_type, target_type=tgt[0], target_id=tgt[1],
                      **defaults)


def make_context(objective="Land a senior platform role"):
    """Covered Terraform capability chaining to all three approved facts;
    Machine Learning targeted but uncovered (gap report only)."""
    evidence = Evidence(id="iev_1", evidence_type="cv", title="cv")
    chains = tuple(
        FactChain(
            proves_edge=_edge(f"iedge_p{i}", "PROVES", ("evidence", "iev_1"),
                              ("career_fact", f.id)),
            fact=f,
            experience=EXP_A if f.experience_id == "iexp_a" else EXP_B)
        for i, f in enumerate((FACT_A1, FACT_A2, FACT_B1)))
    selection = SelectionReport(role_family_id="irf_1", selections=(
        CapabilitySelection(capability=CAP_TF, chains=(
            EvidenceChain(
                supports_edge=_edge("iedge_s", "SUPPORTS", ("evidence", "iev_1"),
                                    ("capability", "icap_tf")),
                evidence=evidence, facts=chains),)),
        CapabilitySelection(capability=CAP_ML, chains=()),
    ))
    return GenerationContext(
        role_family_id="irf_1", selection=selection, profile=dict(PROFILE),
        experiences=(EXP_A, EXP_B),
        strategy=StrategySnapshot(family=FAMILY, strategy_version=1,
                                  objective=objective, allocation=4))


def entry_a(bullets):
    return CvExperienceEntry(
        experience_id="iexp_a", title=EXP_A.title, org=EXP_A.org,
        start_date=EXP_A.start_date, end_date=EXP_A.end_date, bullets=tuple(bullets))


def entry_b(bullets):
    return CvExperienceEntry(
        experience_id="iexp_b", title=EXP_B.title, org=EXP_B.org,
        start_date=EXP_B.start_date, end_date=EXP_B.end_date, bullets=tuple(bullets))


def make_cv(summary="", skills=(SkillItem(name="Terraform", capability_ids=("icap_tf",)),),
            entries=None):
    if entries is None:
        entries = (entry_a((Bullet(text=FACT_A1.statement, fact_ids=("ifact_a1",)),)),)
    return CvModel(
        header=CvHeader(name="Casey Sample", email="casey@sample.dev",
                        phone="+1 555 0100", location="Lisbon, Portugal",
                        links=("https://github.com/casey-sample",)),
        summary=summary, skills=tuple(skills), experiences=tuple(entries),
        meta=CvMeta(role_family_id="irf_1", strategy_version=1,
                    generated_at="2026-08-12T00:00:00Z"))


def rules(cv, context=None):
    report = GroundingVerifier(context or make_context()).verify(cv)
    return {f.rule for f in report.findings}


def test_baseline_grounded_cv_passes():
    """Sanity: the fixture world itself is clean, so every failure below is
    the injected violation, not fixture noise."""
    report = GroundingVerifier(make_context()).verify(make_cv())
    assert report.passed, report.to_json()


# -- polarity-style rewording (not previously covered) -----------------------

def test_polarity_inverted_bullet_fails():
    """A reworded claim that inverts the fact's direction ('reduced' ->
    'increased') introduces an ungrounded content word and must not pass."""
    cv = make_cv(entries=(entry_a((Bullet(
        text="Increased deployment failures by 35% across 8 services by"
             " introducing a Terraform release pipeline",
        fact_ids=("ifact_a1",)),)),))
    assert "content-words" in rules(cv)


def test_polarity_inverted_summary_fails():
    cv = make_cv(summary="Grew deployment failures across services with a"
                         " Terraform release pipeline.")
    assert "content-words" in rules(cv)


# -- scope inflation (independent re-authoring) ------------------------------

def test_scope_inflation_used_never_becomes_built():
    cv = make_cv(entries=(entry_a((Bullet(
        text="Built Terraform staging environments",
        fact_ids=("ifact_a2",)),)),))
    assert "scope-inflation" in rules(cv)


def test_scope_inflation_contributed_never_becomes_led():
    cv = make_cv(entries=(entry_b((Bullet(
        text="Led a churn prediction machine learning model used by the sales team",
        fact_ids=("ifact_b1",)),)),))
    assert "scope-inflation" in rules(cv)


# -- unselected capability can never render as a skill -----------------------

def test_uncovered_targeted_capability_never_renders_as_skill():
    """Machine Learning is targeted but has no eligible chain, and its words
    even appear in an approved fact statement (poisoned context): the skill
    must still be rejected, since TARGETS states relevance, not possession."""
    cv = make_cv(skills=(SkillItem(name="Machine Learning",
                                   capability_ids=("icap_ml",)),))
    assert "skills" in rules(cv)


def test_unknown_capability_id_never_renders_as_skill():
    cv = make_cv(skills=(SkillItem(name="Terraform", capability_ids=("icap_ghost",)),))
    assert "skills" in rules(cv)


def test_skill_display_name_must_be_canonical_row_name():
    cv = make_cv(skills=(SkillItem(name="IaC Wizardry", capability_ids=("icap_tf",)),))
    assert "skills" in rules(cv)


# -- cross-experience misattribution -----------------------------------------

def test_cross_experience_misattribution_fails():
    """A reachable approved fact from Delta Partners can never render under
    the Nimbus entry."""
    cv = make_cv(entries=(entry_a((Bullet(
        text="Contributed to a churn prediction machine learning model used"
             " by the sales team",
        fact_ids=("ifact_b1",)),)),))
    assert "traceability" in rules(cv)


# -- poisoned context / poisoned strategy, per summary check class -----------

def test_poisoned_strategy_entity_never_grounds_summary():
    context = make_context(objective="Join QuantumForge as a principal")
    assert "entities" in rules(
        make_cv(summary="Platform engineer at QuantumForge."), context)


def test_poisoned_strategy_content_word_never_grounds_summary():
    context = make_context(objective="Become a wizard of orchestration")
    assert "content-words" in rules(make_cv(summary="A wizard platform engineer."),
                                    context)


def test_poisoned_strategy_number_never_grounds_summary():
    context = make_context(objective="Own 7 flagship launches by 2050")
    assert "numbers-dates" in rules(
        make_cv(summary="Platform engineer since 2050."), context)


def test_family_vocabulary_never_grounds_summary():
    """The family row (search vocabulary) is strategy control data too."""
    assert "entities" in rules(make_cv(summary="Kubernetes platform engineer."))


# -- experience gate: unapproved-only / unreachable-only ---------------------

def _seeded_conn(tmp_path):
    db = tmp_path / "independent.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute("INSERT INTO role_families (id, name, rationale)"
                     " VALUES ('irf_1', 'Platform', 'r')")
    experiences = SqliteExperienceRepository(conn)
    experiences.add(EXP_A)
    experiences.add(Experience(id="iexp_unapproved", kind="role",
                               title="Intern", org="OldCo"))
    experiences.add(Experience(id="iexp_unreachable", kind="role",
                               title="Barista", org="CafeCo"))
    caps = SqliteCapabilityRepository(conn)
    caps.add(CAP_TF)
    caps.add(Capability(id="icap_off", name="Latte Art", strength="strong"))
    ev = SqliteEvidenceRepository(conn)
    ev.add(Evidence(id="iev_1", evidence_type="cv", title="cv"))
    ev.add(Evidence(id="iev_2", evidence_type="cv", title="other cv"))
    facts = SqliteCareerFactRepository(conn)
    facts.add(FACT_A1)
    # Approved fact, but its whole chain hangs off a capability this family
    # does not target: unreachable by the walk.
    facts.add(CareerFact(id="ifact_unreach", fact_type="achievement",
                         statement="Poured five hundred lattes",
                         source="interview", user_approved=1,
                         experience_id="iexp_unreachable"))
    # Unapproved fact on the targeted chain.
    facts.add(CareerFact(id="ifact_unapp", fact_type="achievement",
                         statement="Single-handedly invented Terraform",
                         source="cv", user_approved=0,
                         experience_id="iexp_unapproved"))
    edges = SqliteCareerEdgeRepository(conn)
    edges.add(_edge("iedge_t", "TARGETS", ("role_family", "irf_1"),
                    ("capability", "icap_tf")))
    edges.add(_edge("iedge_s1", "SUPPORTS", ("evidence", "iev_1"),
                    ("capability", "icap_tf")))
    edges.add(_edge("iedge_p1", "PROVES", ("evidence", "iev_1"),
                    ("career_fact", "ifact_a1")))
    edges.add(_edge("iedge_p_unapp", "PROVES", ("evidence", "iev_1"),
                    ("career_fact", "ifact_unapp")))
    edges.add(_edge("iedge_s2", "SUPPORTS", ("evidence", "iev_2"),
                    ("capability", "icap_off")))
    edges.add(_edge("iedge_p_unreach", "PROVES", ("evidence", "iev_2"),
                    ("career_fact", "ifact_unreach")))
    return conn


def _select_and_build(conn):
    edges = SqliteCareerEdgeRepository(conn)
    traversal = EvidenceTraversal(
        edges, SqliteEvidenceRepository(conn), SqliteCareerFactRepository(conn),
        SqliteExperienceRepository(conn))
    selection = FamilyEvidenceSelection(
        edges, SqliteCapabilityRepository(conn), traversal).select("irf_1")
    context = GenerationContext(
        role_family_id="irf_1", selection=selection, profile=dict(PROFILE),
        experiences=tuple(SqliteExperienceRepository(conn).list_all()),
        strategy=StrategySnapshot(family=FAMILY, strategy_version=1,
                                  objective="obj", allocation=4))
    cv, _dropped = build_verbatim_model(context, "2026-08-12T00:00:00Z")
    return context, cv


def test_experience_with_only_unapproved_facts_never_renders(tmp_path):
    conn = _seeded_conn(tmp_path)
    try:
        context, cv = _select_and_build(conn)
        rendered = {e.experience_id for e in cv.all_entries()}
        assert "iexp_unapproved" not in rendered
        assert rendered == {"iexp_a"}
        assert "ifact_unapp" not in context.renderable_grounding_view()["facts"]
        # Defense in depth: hand-forcing the entry past the builder still fails.
        forced = make_cv(entries=(
            entry_a((Bullet(text=FACT_A1.statement, fact_ids=("ifact_a1",)),)),
            CvExperienceEntry(experience_id="iexp_unapproved", title="Intern",
                              org="OldCo", start_date=None, end_date=None,
                              bullets=(Bullet(text="Single-handedly invented Terraform",
                                              fact_ids=("ifact_unapp",)),)),))
        report = GroundingVerifier(context).verify(forced)
        assert any(f.rule == "traceability" and "ifact_unapp" in f.message
                   for f in report.findings), report.to_json()
    finally:
        conn.close()


def test_experience_with_only_unreachable_approved_facts_never_renders(tmp_path):
    conn = _seeded_conn(tmp_path)
    try:
        context, cv = _select_and_build(conn)
        rendered = {e.experience_id for e in cv.all_entries()}
        assert "iexp_unreachable" not in rendered
        # No exception lane: approved but unreachable does not qualify.
        assert "ifact_unreach" not in context.selection.fact_ids()
        forced = make_cv(entries=(
            entry_a((Bullet(text=FACT_A1.statement, fact_ids=("ifact_a1",)),)),
            CvExperienceEntry(experience_id="iexp_unreachable", title="Barista",
                              org="CafeCo", start_date=None, end_date=None,
                              bullets=(Bullet(text="Poured five hundred lattes",
                                              fact_ids=("ifact_unreach",)),)),))
        report = GroundingVerifier(context).verify(forced)
        assert any(f.rule == "traceability" and "ifact_unreach" in f.message
                   for f in report.findings), report.to_json()
    finally:
        conn.close()


def test_experience_without_rendered_bullets_never_renders(tmp_path):
    """The gate is 'attaches AND renders': an entry with zero bullets fails."""
    conn = _seeded_conn(tmp_path)
    try:
        context, _cv = _select_and_build(conn)
        forced = make_cv(entries=(
            entry_a((Bullet(text=FACT_A1.statement, fact_ids=("ifact_a1",)),)),
            CvExperienceEntry(experience_id="iexp_unreachable", title="Barista",
                              org="CafeCo", start_date=None, end_date=None,
                              bullets=()),))
        report = GroundingVerifier(context).verify(forced)
        assert any(f.rule == "experience-gate" for f in report.findings)
    finally:
        conn.close()
