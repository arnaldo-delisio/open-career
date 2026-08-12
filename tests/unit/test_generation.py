"""Drafting loop: reject-and-retry with the failure named, verbatim fallback
(spec: decisions/package-generation-design.md)."""

import json

from domain.generation import (
    MAX_DRAFT_ATTEMPTS,
    CvDraftingService,
    build_verbatim_model,
)
from dataclasses import replace

from domain.grounding import GroundingVerifier
from domain.ports import ModelAdapter
from tests.unit.test_grounding import EDU, EXP, EXP2, make_context, make_cv


class FakeModel(ModelAdapter):
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def test_verbatim_fallback_is_grounded_by_construction():
    context = make_context()
    cv, dropped = build_verbatim_model(context, "2026-08-11T00:00:00Z")
    report = GroundingVerifier(context).verify(cv)
    assert report.passed, report.to_json()
    assert dropped == ("summary",)
    assert cv.summary == ""
    fact_ids = {fid for e in cv.all_entries() for b in e.bullets for fid in b.fact_ids}
    assert fact_ids == {"fact_1", "fact_2"}


def test_retry_carries_the_named_failure():
    context = make_context()
    bad = make_cv(summary="Visionary blockchain evangelist.")
    good = make_cv()
    model = FakeModel([bad.to_json(), good.to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 2 and not result.fallback_used
    assert "content-words" in model.prompts[1]  # the failure travels back, named


def test_fallback_after_bounded_retries():
    context = make_context()
    bad = make_cv(summary="Visionary blockchain evangelist.")
    model = FakeModel([bad.to_json()] * MAX_DRAFT_ATTEMPTS)
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.fallback_used and result.report.passed
    assert result.attempts == MAX_DRAFT_ATTEMPTS
    trail = json.loads(result.trail_json())
    assert trail["fallback_used"] is True and trail["dropped"] == ["summary"]


def test_schema_invalid_output_retries():
    context = make_context()
    model = FakeModel(["not json at all", make_cv().to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 2
    assert "schema validation" in model.prompts[1]


def test_model_output_cannot_omit_or_alter_the_header():
    """The drafted header is rebuilt in code from user_profile: omitted or
    altered contact fields never survive parsing."""
    context = make_context()
    tampered = replace(make_cv(),
                       header=replace(make_cv().header, email=None,
                                      phone="+1 555 0000", links=()))
    model = FakeModel([tampered.to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 1
    header = result.cv.header
    assert header.email == "t@example.com"
    assert header.phone == "+39 333 1234567"
    assert header.links == ("https://github.com/example",)


def test_verbatim_model_renders_factless_education_skeleton_only():
    """A confirmed factless education row renders skeleton-only; a factless
    employment row still never renders (container gate)."""
    ghost = replace(EXP2, id="exp_ghost", title="Ghost Role", org="GhostCo")
    context = make_context(experiences=(EXP, EXP2, EDU, ghost))
    cv, _dropped = build_verbatim_model(context, "2026-08-11T00:00:00Z")
    assert [e.experience_id for e in cv.education] == ["exp_edu"]
    assert cv.education[0].bullets == ()
    assert cv.education[0].title == "B.Sc. Computer Science"
    assert "exp_ghost" not in {e.experience_id for e in cv.experiences}
    report = GroundingVerifier(context).verify(cv)
    assert report.passed, report.to_json()


def test_model_draft_omitting_factless_education_is_corrected():
    """Successful model path: a draft that omits a confirmed factless
    education row fails verification with the coverage rule named, and the
    corrected retry carries the row."""
    context = make_context(experiences=(EXP, EXP2, EDU))
    incomplete = make_cv()  # exp_1 only, no education section
    complete, _dropped = build_verbatim_model(context, "2026-08-11T00:00:00Z")
    model = FakeModel([incomplete.to_json(), complete.to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 2 and not result.fallback_used
    assert "coverage" in model.prompts[1]
    assert [e.experience_id for e in result.cv.education] == ["exp_edu"]


def test_prompt_contains_context_snapshot():
    context = make_context()
    model = FakeModel([make_cv().to_json()])
    CvDraftingService(model, "PROMPT {context_json}").draft(context, "2026-08-11T00:00:00Z")
    assert context.snapshot_hash() is not None
    assert json.loads(context.snapshot_json())["strategy"]["objective"] in model.prompts[0]
