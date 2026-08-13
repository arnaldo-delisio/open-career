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


def test_metric_catchup_never_asks_on_non_quantifiable_fact_types(tmp_path):
    """'other' facts (exclusion lists, stances) are not quantifiable: the
    catch-up pass skips them entirely (drive finding)."""
    conn = _conn(tmp_path)
    try:
        _approved_fact(conn, "Never adtech", fact_type="other")
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return ""

        run_metric_catchup(conn, ask=ask, say=lambda _: None)
        assert prompts == []  # nothing asked
        fact = SqliteCareerFactRepository(conn).list_all()[0]
        assert fact.statement == "Never adtech"
    finally:
        conn.close()


def test_yes_no_prompt_reprompts_on_garbage_and_consumes_nothing():
    """A y/n prompt validates like every enum prompt: garbage re-asks and is
    never consumed as data (drive finding: a capability name fell into one)."""
    from apps.cli.interview import ask_yes_no

    says = []
    answers = iter(["backend design", "y"])
    assert ask_yes_no(lambda _p: next(answers), says.append,
                      "Link it?", default=False) is True
    assert says == ["invalid choice, expected y/n"]
    assert next(answers, "exhausted") == "exhausted"  # both answers consumed by the prompt


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


def test_ctrl_c_mid_interview_exits_130_with_saved_note(tmp_path, monkeypatch, capsys):
    """KeyboardInterrupt at the CLI entry is a one-line goodbye, exit 130,
    never a traceback: every answer already persisted (drive finding)."""
    import pytest

    from adapters.storage.migrations import migrate as _migrate
    from apps.cli.main import main

    instance = tmp_path / "instance"
    _migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))

    def interrupted(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    with pytest.raises(SystemExit) as exc:
        main(["deepen"])
    assert exc.value.code == 130
    out = capsys.readouterr()
    assert "interrupted; everything answered so far is saved" in out.out
    assert "Traceback" not in out.err


def test_non_interview_interrupt_keeps_default_behavior(tmp_path, monkeypatch, capsys):
    """The progress-saved message is honest only for persist-as-you-go
    interview commands; an interrupted export must not print it
    (Codex round 5)."""
    import pytest

    from apps.cli import main as main_module

    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "export_to_file", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main_module.main(["export", str(tmp_path / "dump.json")])
    assert "everything answered so far is saved" not in capsys.readouterr().out


def test_hard_exclusions_merge_is_ordered_unique(tmp_path):
    """Duplicates inside one comma-separated answer collapse, and
    already-present duplicates collapse on the next merge, first occurrence
    winning (Codex round 5)."""
    from apps.cli.interview import _ask_hard_exclusions

    conn = _conn(tmp_path)
    try:
        policies = SqliteUserPolicyRepository(conn)
        _ask_hard_exclusions(policies, ask=_scripted(["adtech, gambling, adtech"]),
                             say=lambda _: None)
        assert policies.get_policies()["industry_pref"]["out"] == ["adtech", "gambling"]

        # Pre-existing duplicates (written directly through the seam) collapse
        # the next time the merge runs.
        policies.set_policy("industry_pref",
                            {"in": [], "out": ["adtech", "adtech", "gambling"]},
                            source="user_edit")
        _ask_hard_exclusions(policies, ask=_scripted(["defense"]), say=lambda _: None)
        assert policies.get_policies()["industry_pref"]["out"] == [
            "adtech", "gambling", "defense"]
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
        "",                                         # never_render skipped
        "always_decline",                           # eeo_stance
        # still-unset tier-1 fields (all 10 unset here): skip all
        "", "", "", "", "", "", "", "", "", "",
        # stated facts: languages, travel
        "English C2, Italian native", "",
        # hard exclusions -> industry_pref.out (one home, no fact)
        "adtech, gambling",
        # evidence intake: one repository, then finish
        "repository", "open-career", "https://github.com/example/open-career",
        "Built a career graph with 300 tests",       # fact offered ('other': no quantifier ask)
        "",                                          # intake done
        # metric catch-up: nothing quantifiable remains unquantified
    ]
    conn = _conn(tmp_path)
    try:
        run_deepen(conn, ask=_scripted(answers), say=lambda _: None)
        fields = SqliteUserProfileRepository(conn).get_fields()
        assert fields == {"linkedin_url": "https://linkedin.com/in/jane"}
        policies = SqliteUserPolicyRepository(conn).get_policies()
        assert policies["eeo_stance"] == "always_decline"
        # Hard exclusions have one home: industry_pref.out, never a fact.
        assert policies["industry_pref"] == {"in": [], "out": ["adtech", "gambling"]}
        facts = SqliteCareerFactRepository(conn).list_all()
        statements = {f.statement for f in facts}
        assert "English C2, Italian native" in statements
        assert "Built a career graph with 300 tests" in statements
        assert not any("adtech" in s for s in statements)
        assert all(f.user_approved and f.source == "interview" for f in facts)
        evidence = SqliteEvidenceRepository(conn).list_all()
        types = sorted(e.evidence_type for e in evidence)
        assert types == ["repository", "user_statement"]
    finally:
        conn.close()


def test_a_restatement_without_a_number_never_overwrites_the_fact(tmp_path):
    """The drive typed 'confirm' at the metric prompt and it silently replaced
    a real achievement, which then rode into the exported PDF as a bullet
    reading 'confirm'. The prompt asks for a number, so it validates for one;
    the approved text survives an answer that has none."""
    conn = _conn(tmp_path)
    try:
        fact = _approved_fact(conn, "Ran the client onboarding process")
        says = []
        # 'confirm' is refused, the second answer is blank (skip).
        run_metric_catchup(conn, ask=_scripted(["confirm", ""]), say=says.append)
        stored = SqliteCareerFactRepository(conn).list_all()[0]
        assert stored.statement == "Ran the client onboarding process"
        assert any("no number in it either" in s for s in says)
    finally:
        conn.close()


def test_blank_at_the_metric_prompt_skips_exactly_as_promised(tmp_path):
    conn = _conn(tmp_path)
    try:
        _approved_fact(conn, "Ran the client onboarding process")
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return ""

        run_metric_catchup(conn, ask=ask, say=lambda _: None)
        assert len(prompts) == 1  # asked once, skipped, not re-asked
        assert (SqliteCareerFactRepository(conn).list_all()[0].statement
                == "Ran the client onboarding process")
    finally:
        conn.close()


def test_tier1_accepts_y_n_and_stores_the_canonical_words(tmp_path):
    """Answering 'y' to a (yes/no) prompt used to persist 'y' and produce a
    package the Gauntlet could never pass (drive finding)."""
    answers = [
        "Italy", "y", "n", "remote", "y", "1 month",
        "", "",  # both compensation policies skipped
    ]
    conn = _conn(tmp_path)
    try:
        run_tier1(conn, ask=_scripted(answers), say=lambda _: None)
        fields = SqliteUserProfileRepository(conn).get_fields()
        assert fields["authorized_in_country"] == "yes"
        assert fields["needs_sponsorship"] == "no"
        assert fields["relocation"] == "yes"
    finally:
        conn.close()


def test_tier1_re_asks_an_answer_outside_the_closed_set(tmp_path):
    answers = [
        "Italy", "maybe", "yes", "no", "remote", "no", "1 month", "", "",
    ]
    conn = _conn(tmp_path)
    try:
        says = []
        run_tier1(conn, ask=_scripted(answers), say=says.append)
        fields = SqliteUserProfileRepository(conn).get_fields()
        assert fields["authorized_in_country"] == "yes"
        assert any("is not an answer to 'authorized_in_country'" in s for s in says)
    finally:
        conn.close()


def test_tier1_names_the_authorization_contradiction_and_offers_a_revision(tmp_path):
    """Authorized AND needing sponsorship read as opposites downstream. The
    conflict is named while the human is still here; the answers are never
    silently corrected."""
    answers = [
        "Italy", "yes", "yes", "remote", "no", "1 month",
        "n",           # no, do not keep both as given
        "yes", "no",   # revised authorization answers
        "", "",        # compensation policies skipped
    ]
    conn = _conn(tmp_path)
    try:
        says = []
        run_tier1(conn, ask=_scripted(answers), say=says.append)
        assert any("need visa sponsorship" in s for s in says)
        fields = SqliteUserProfileRepository(conn).get_fields()
        assert (fields["authorized_in_country"], fields["needs_sponsorship"]) == ("yes", "no")
    finally:
        conn.close()
