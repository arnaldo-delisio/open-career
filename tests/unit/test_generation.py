"""Drafting loop: reject-and-retry with the failure named, verbatim fallback
(spec: decisions/package-generation-design.md)."""

import json

import pytest

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


def test_model_cannot_stamp_meta_identity_fields():
    """role_family_id and strategy_version come from the context in code: an
    otherwise grounded draft carrying wrong values is corrected, not trusted."""
    context = make_context()
    good = make_cv()
    tampered = replace(good, meta=replace(good.meta, role_family_id="rf_evil",
                                          strategy_version=99,
                                          generated_at="1999-01-01T00:00:00Z"))
    model = FakeModel([tampered.to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 1
    assert result.cv.meta.role_family_id == "rf_1"
    assert result.cv.meta.strategy_version == 1
    assert result.cv.meta.generated_at == "2026-08-11T00:00:00Z"  # the caller's clock


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


def test_header_only_husk_fails_and_is_corrected():
    """An otherwise-valid model response with empty summary, skills, and
    experiences must not verify when the walk reached experience-backed
    facts; the retry names the finding."""
    context = make_context()
    good = make_cv()
    husk = replace(good, summary="", skills=(), experiences=())
    model = FakeModel([husk.to_json(), good.to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 2 and not result.fallback_used
    assert "husk" in model.prompts[1]
    assert result.cv.experiences


def test_prompt_contains_context_snapshot():
    context = make_context()
    model = FakeModel([make_cv().to_json()])
    CvDraftingService(model, "PROMPT {context_json}").draft(context, "2026-08-11T00:00:00Z")
    assert context.snapshot_hash() is not None
    assert json.loads(context.snapshot_json())["strategy"]["objective"] in model.prompts[0]


def test_a_non_string_date_field_fails_schema_validation_not_the_render():
    """The renderer reads dates as labels, so a number or an object arriving
    in one is a schema failure with the field named, never an AttributeError
    at render time."""
    from domain.cv_model import CvModelError, parse_cv_model

    payload = json.loads(make_cv().to_json())
    payload["experiences"][0]["end_date"] = 2024
    with pytest.raises(CvModelError, match="end_date: must be a string or null"):
        parse_cv_model(json.dumps(payload))
    payload["experiences"][0]["end_date"] = None
    payload["experiences"][0]["org"] = {"name": "Acme"}
    with pytest.raises(CvModelError, match="org: must be a string or null"):
        parse_cv_model(json.dumps(payload))
    # The retry loop treats it as any other schema failure, named back.
    context = make_context()
    model = FakeModel([json.dumps(payload), make_cv().to_json()])
    result = CvDraftingService(model, "PROMPT {context_json}").draft(
        context, "2026-08-11T00:00:00Z")
    assert result.report.passed and result.attempts == 2
    assert "schema validation" in model.prompts[1] and "org" in model.prompts[1]
