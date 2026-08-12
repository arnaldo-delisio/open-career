"""Drafting loop: reject-and-retry with the failure named, verbatim fallback
(spec: decisions/package-generation-design.md)."""

import json

from domain.generation import (
    MAX_DRAFT_ATTEMPTS,
    CvDraftingService,
    build_verbatim_model,
)
from domain.grounding import GroundingVerifier
from domain.ports import ModelAdapter
from tests.unit.test_grounding import make_context, make_cv


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


def test_prompt_contains_context_snapshot():
    context = make_context()
    model = FakeModel([make_cv().to_json()])
    CvDraftingService(model, "PROMPT {context_json}").draft(context, "2026-08-11T00:00:00Z")
    assert context.snapshot_hash() is not None
    assert json.loads(context.snapshot_json())["strategy"]["objective"] in model.prompts[0]
