"""Registry-generated interview flows (OC-35): sitting one's must-ask block,
`deepen`, and the metric backfill, driven with scripted answers."""

import sqlite3

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_entities import (
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
)
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.interview import run_deepen, run_metric_catchup, run_tier1
from apps.cli.onboarding import run_onboarding
from domain.entities import CareerFact
from domain.ids import new_id


def _scripted(answers):
    remaining = list(answers)
    return lambda _prompt: remaining.pop(0)


def _conn(tmp_path):
    migrate(tmp_path / "open-career.sqlite3")
    conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_tier1_asks_the_must_ask_block_and_persists_through_the_seams(tmp_path):
    answers = [
        "Italy",            # country
        "yes",              # authorized_in_country
        "no",               # needs_sponsorship
        "remote",           # remote_preference
        "yes",              # relocation
        "1 month",          # notice_period
        "60000", "EUR", "annual",            # compensation floor
        "65000", "85000", "EUR", "annual", "mid",  # compensation target + scalar
    ]
    conn = _conn(tmp_path)
    try:
        run_tier1(conn, ask=_scripted(answers), say=lambda _: None)
        fields = SqliteUserProfileRepository(conn).get_fields()
        assert fields == {"country": "Italy", "authorized_in_country": "yes",
                          "needs_sponsorship": "no", "remote_preference": "remote",
                          "relocation": "yes", "notice_period": "1 month"}
        policies = SqliteUserPolicyRepository(conn).get_policies()
        assert policies["compensation_floor"] == {
            "amount": 60000, "currency": "EUR", "period": "annual"}
        assert policies["compensation_target"] == {
            "min": 65000, "max": 85000, "currency": "EUR", "period": "annual",
            "scalar": "mid"}
        # Every write went through the audited seams.
        assert len(SqliteUserProfileRepository(conn).list_writes()) == 6
        assert len(SqliteUserPolicyRepository(conn).list_writes()) == 2
    finally:
        conn.close()


def test_tier1_blank_skips_everything_and_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    try:
        run_tier1(conn, ask=lambda _prompt: "", say=lambda _: None)
        assert SqliteUserProfileRepository(conn).get_fields() == {}
        assert SqliteUserPolicyRepository(conn).get_policies() == {}
    finally:
        conn.close()


def test_tier1_shows_current_values(tmp_path):
    """Every question shows its current value; a blank keeps it."""
    conn = _conn(tmp_path)
    try:
        SqliteUserProfileRepository(conn).set_field("country", "Italy", source="user_edit")
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return ""

        run_tier1(conn, ask=ask, say=lambda _: None)
        assert any("[Italy]" in p for p in prompts)
        assert SqliteUserProfileRepository(conn).get_fields()["country"] == "Italy"
    finally:
        conn.close()


def test_tier1_rejects_a_below_min_target_max(tmp_path):
    answers = ["", "", "", "", "", "",   # profile block skipped
               "",                        # floor skipped
               "70000", "60000", "80000", "EUR", "annual", "min"]  # max re-asked
    says = []
    conn = _conn(tmp_path)
    try:
        run_tier1(conn, ask=_scripted(answers), say=says.append)
        target = SqliteUserPolicyRepository(conn).get_policies()["compensation_target"]
        assert (target["min"], target["max"], target["scalar"]) == (70000, 80000, "min")
        assert any("must be at least 70000" in s for s in says)
    finally:
        conn.close()


def _approved_fact(conn, statement, fact_type="achievement"):
    fact = CareerFact(id=new_id("fact"), fact_type=fact_type, statement=statement,
                      source="interview", user_approved=1, verified_at="2026-08-12T00:00:00Z")
    SqliteCareerFactRepository(conn).add(fact)
    return fact


def test_metric_catchup_revisits_only_unquantified_facts(tmp_path):
    conn = _conn(tmp_path)
    try:
        plain = _approved_fact(conn, "Ran the client onboarding process")
        numbered = _approved_fact(conn, "Onboarded 40 clients")
        answers = ["Ran onboarding for 12 clients"]  # one ask, for the plain fact only
        run_metric_catchup(conn, ask=_scripted(answers), say=lambda _: None)
        facts = {f.id: f for f in SqliteCareerFactRepository(conn).list_all()}
        assert facts[plain.id].statement == "Ran onboarding for 12 clients"
        assert facts[numbered.id].statement == "Onboarded 40 clients"
    finally:
        conn.close()


def test_inline_backfill_in_the_cv_fact_walk_is_a_user_edit(tmp_path):
    """Confirming an unquantified fact offers one follow-up; the restatement is
    a user edit, approved. A skipped follow-up changes nothing (spec: metric
    backfill; the system never proposes a number)."""
    import json

    from domain.ports import ModelAdapter

    extraction = json.dumps({
        "experiences": [],
        "facts": [
            {"experience_index": None, "fact_type": "scope",
             "statement": "Led a team", "source_location": None},
            {"experience_index": None, "fact_type": "achievement",
             "statement": "Shipped 3 services", "source_location": None},
        ],
    })

    class Model(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return extraction

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    prompts = []
    answers = [
        "confirm", "Led a team of 6",  # unquantified: follow-up, restated
        "confirm",                     # quantified: no follow-up asked
        "", "", "", "", "", "",        # capabilities, goals, basics
    ]
    remaining = list(answers)

    def ask(prompt):
        prompts.append(prompt)
        return remaining.pop(0)

    conn = _conn(tmp_path)
    try:
        run_onboarding(conn, LocalStorageAdapter(tmp_path), Model(), cv,
                       ask=ask, say=lambda _: None)
        statements = {f.statement for f in SqliteCareerFactRepository(conn).list_all()}
        assert "Led a team of 6" in statements
        assert "Shipped 3 services" in statements
        assert sum("No number in that fact" in p for p in prompts) == 1
    finally:
        conn.close()


def test_deepen_walks_tier2_stance_facts_evidence_and_catchup(tmp_path):
    answers = [
        # 17 tier-2 profile fields, in registry order; only two answered
        "", "",                                     # preferred_name, pronouns
        "https://linkedin.com/in/jane",             # linkedin_url
        "", "", "",                                 # github, portfolio, website
        "", "", "", "",                             # company, title, consents
        "", "", "", "", "", "", "",                 # 7 EEO fields skipped
        "always_decline",                           # eeo_stance
        # still-unset tier-1 fields (all 10 unset here): skip all
        "", "", "", "", "", "", "", "", "", "",
        # stated facts: languages, travel, hard exclusions
        "English C2, Italian native", "", "Never adtech",
        # evidence intake: one repository, then finish
        "repository", "open-career", "https://github.com/example/open-career",
        "Built a career graph with 300 tests", "",   # fact offered, quantifier has digits
        "",                                          # intake done
        # metric catch-up: no approved unquantified facts remain (the two stated
        # facts carry no digits... they do not; catch-up asks for each)
        "", "",
    ]
    conn = _conn(tmp_path)
    try:
        run_deepen(conn, ask=_scripted(answers), say=lambda _: None)
        fields = SqliteUserProfileRepository(conn).get_fields()
        assert fields == {"linkedin_url": "https://linkedin.com/in/jane"}
        assert SqliteUserPolicyRepository(conn).get_policies()["eeo_stance"] == "always_decline"
        facts = SqliteCareerFactRepository(conn).list_all()
        statements = {f.statement for f in facts}
        assert "English C2, Italian native" in statements
        assert "Never adtech" in statements
        assert "Built a career graph with 300 tests" in statements
        assert all(f.user_approved and f.source == "interview" for f in facts)
        evidence = SqliteEvidenceRepository(conn).list_all()
        types = sorted(e.evidence_type for e in evidence)
        assert types == ["repository", "user_statement"]
    finally:
        conn.close()
