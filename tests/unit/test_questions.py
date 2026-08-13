"""The typed question registry: completeness over the canonical field and
policy sets, every entry with an intentional disposition (OC-35)."""

from apps.cli.stories import LOGISTICS_POLICY_KEYS, PREFERENCE_POLICY_KEYS
from domain.policies import CANONICAL_POLICY_KEYS
from domain.profile import CANONICAL_PROFILE_FIELDS
from domain.questions import (
    DEPTH,
    FLOOR,
    QUESTION_REGISTRY,
    TIER1,
    TIER2,
    questions,
    registry_coverage,
)


def test_every_field_and_policy_has_exactly_one_disposition():
    """The completeness contract: all 28 canonical fields (phone included) and
    all 12 policies appear exactly once, each with a valid tier."""
    profile_keys, policy_keys = registry_coverage()
    assert profile_keys == CANONICAL_PROFILE_FIELDS
    assert policy_keys == CANONICAL_POLICY_KEYS
    assert len(QUESTION_REGISTRY) == len(CANONICAL_PROFILE_FIELDS) + len(CANONICAL_POLICY_KEYS)
    assert all(q.tier in (TIER1, TIER2, DEPTH, FLOOR) for q in QUESTION_REGISTRY)


def test_every_question_names_its_consumer():
    assert all(q.prompt and q.consumer for q in QUESTION_REGISTRY)


def test_tier1_is_the_must_ask_block():
    """Sitting one: identity/contact basics plus what unblocks applying at all
    (work authorization, location logistics, notice period, compensation)."""
    assert {q.key for q in questions(kind="profile", tier=TIER1)} == {
        "full_name", "email", "phone", "location", "country",
        "authorized_in_country", "needs_sponsorship", "remote_preference",
        "relocation", "notice_period"}
    assert {q.key for q in questions(kind="policy", tier=TIER1)} == {
        "compensation_floor", "compensation_target"}


def test_links_and_eeo_are_tier2_they_block_nothing():
    tier2 = {q.key for q in questions(kind="profile", tier=TIER2)}
    assert {"linkedin_url", "github_url", "portfolio_url", "website_url"} <= tier2
    assert {q.key for q in QUESTION_REGISTRY if q.key.startswith("eeo_")
            and q.kind == "profile"} <= tier2
    assert {q.key for q in questions(kind="policy", tier=TIER2)} == {
        "eeo_stance", "never_render"}


def test_salary_expectation_is_deliberately_floor_routed():
    """Compensation lives in the floor/target policies; the canonical free-text
    field is never interviewed (one fact, one home) and falls to the floor."""
    (question,) = [q for q in QUESTION_REGISTRY if q.key == "salary_expectation"]
    assert question.tier == FLOOR


def test_every_tiered_policy_has_an_interview_handler():
    """A policy in TIER1/TIER2 must have a registry-keyed asker, so moving a
    policy into a sitting cannot pass completeness yet never be asked
    (Codex round 1)."""
    from apps.cli.interview import _POLICY_ASKERS

    tiered = {q.key for q in QUESTION_REGISTRY
              if q.kind == "policy" and q.tier in (TIER1, TIER2)}
    assert tiered <= set(_POLICY_ASKERS)


def test_depth_policies_are_exactly_the_stories_clusters():
    """Every DEPTH policy is owned by a stories cluster (preferences or
    logistics), so a new depth policy cannot silently gain no question."""
    depth = {q.key for q in questions(kind="policy", tier=DEPTH)}
    assert depth == set(PREFERENCE_POLICY_KEYS) | set(LOGISTICS_POLICY_KEYS)
