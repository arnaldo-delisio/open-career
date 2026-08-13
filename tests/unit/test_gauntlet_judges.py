"""Judge mechanics tests (spec: the scope's decisions/gauntlet-design.md,
"Judge mechanics"): closed output schema with bounded retry, the finding
evidence grammar, and the deterministic outcome mapping."""

import json

import pytest
from test_gauntlet_invariants import FACT, PROFILE, make_cv, make_snapshot

from domain.gauntlet_judges import (
    FAIL,
    OPERATIONAL_ABSTAIN,
    PASS,
    TERMINAL_ABSTAIN,
    build_judge_payload,
    build_judge_prompt,
    element_texts,
    run_judge,
    validate_findings,
)
from domain.ports import ModelAdapter, ModelUnavailableError


class FakeJudgeModel(ModelAdapter):
    def __init__(self, responses, model="fake-model-1"):
        self.responses = list(responses)
        # A string reports the same identity every call; a list reports one
        # identity per call (the model-change-between-retries case).
        self.models = list(model) if isinstance(model, list) else None
        self.model = model if isinstance(model, str) else None
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self.responses.pop(0)

    def complete_with_meta(self, prompt: str):
        raw = self.complete(prompt)
        model = self.models.pop(0) if self.models is not None else self.model
        return raw, {"model": model}


def _output(verdict="PASS", findings=()):
    return json.dumps({"verdict": verdict, "findings": list(findings)})


BULLET_ID = "experiences[exp_1].bullet[0]"


def _finding(judge="writing", **overrides):
    base = {"element_id": BULLET_ID, "severity": "blocking",
            "quote": "Reduced onboarding time by 40%",
            "message": "m"}
    if judge == "truth":
        base["fact_ids"] = ["fact_1"]
    base.update(overrides)
    return base


# -- element map --------------------------------------------------------------

def test_element_texts_covers_summary_bullets_entries_skills_profile():
    cv = make_cv(summary="Engineer who ships")
    elements = element_texts(cv, PROFILE)
    assert elements["summary"] == "Engineer who ships"
    assert elements[BULLET_ID] == FACT
    assert "Forward Deployed Engineer" in elements["experiences[exp_1]"]
    assert elements["skills[Python]"] == "Python"
    assert elements["profile.location"] == "Milan, Italy"


# -- evidence grammar ---------------------------------------------------------

def test_unknown_element_and_foreign_quote_are_dropped():
    cv = make_cv()
    valid, invalid = validate_findings("writing", [
        _finding(element_id="experiences[ghost].bullet[0]"),
        _finding(quote="text that appears nowhere in the element"),
        _finding(),
    ], cv, PROFILE)
    assert len(valid) == 1 and len(invalid) == 2


def test_quote_matches_under_grounding_normalization_not_bytes():
    cv = make_cv()
    valid, invalid = validate_findings("writing", [
        _finding(quote="Reduced  onboarding time by 40%")], cv, PROFILE)
    assert len(valid) == 1 and not invalid


def test_truth_fact_pairing_must_equal_the_bullets_traceability_list():
    cv = make_cv()
    valid, invalid = validate_findings("truth", [
        _finding("truth"),
        _finding("truth", fact_ids=["fact_other"]),
        _finding("truth", fact_ids=[]),
    ], cv, PROFILE)
    assert len(valid) == 1 and valid[0].fact_ids == ("fact_1",)
    assert len(invalid) == 2


def test_truth_summary_findings_carry_no_fact_ids():
    cv = make_cv(summary="Engineer who ships")
    valid, invalid = validate_findings("truth", [
        {"element_id": "summary", "severity": "blocking",
         "quote": "Engineer who ships", "message": "m"},
        {"element_id": "summary", "severity": "blocking",
         "quote": "Engineer who ships", "message": "m", "fact_ids": ["fact_1"]},
    ], cv, PROFILE)
    assert len(valid) == 1 and len(invalid) == 1


def test_consistency_requires_two_distinct_elements_with_own_spans():
    cv = make_cv(summary="Engineer who ships")
    good = {"element_id": "summary", "quote": "Engineer who ships",
            "second_element_id": "profile.location", "second_quote": "Milan",
            "severity": "blocking", "message": "m"}
    same = dict(good, second_element_id="summary")
    foreign = dict(good, second_quote="Tokyo")
    valid, invalid = validate_findings("consistency", [good, same, foreign],
                                       cv, PROFILE)
    assert len(valid) == 1 and valid[0].second_element_id == "profile.location"
    assert len(invalid) == 2


# -- schema validation and bounded retry --------------------------------------

def test_invalid_json_retries_with_failure_named_then_succeeds():
    cv = make_cv()
    model = FakeJudgeModel(["not json", _output()])
    result = run_judge("writing", model, "PROMPT", cv, PROFILE)
    assert result.outcome == PASS and result.attempts == 2
    assert result.model == "fake-model-1"
    # The ordered attempt transcript: the retry prompt carries the named
    # failure and each entry pairs the prompt actually sent with its
    # completion.
    assert len(result.transcript) == 2
    assert result.transcript[0]["prompt"] == "PROMPT"
    assert "failed schema validation" in result.transcript[1]["prompt"]
    assert result.transcript[1]["completion"] == _output()
    assert result.models == ("fake-model-1", "fake-model-1")


def test_schema_retry_exhaustion_is_operational_abstain_never_a_pass():
    cv = make_cv()
    model = FakeJudgeModel(['{"verdict": "MAYBE"}'] * 3)
    result = run_judge("writing", model, "PROMPT", cv, PROFILE)
    assert result.outcome == OPERATIONAL_ABSTAIN and result.attempts == 3
    assert model.calls == 3  # bounded


def test_unknown_keys_and_fail_without_findings_are_schema_errors():
    cv = make_cv()
    model = FakeJudgeModel([
        json.dumps({"verdict": "PASS", "findings": [], "score": 7}),
        _output("FAIL"),
        _output(),
    ])
    result = run_judge("writing", model, "PROMPT", cv, PROFILE)
    assert result.outcome == PASS and result.attempts == 3


def test_adapter_failure_is_operational_abstain():
    class DeadAdapter(ModelAdapter):
        def complete(self, prompt: str) -> str:
            raise ModelUnavailableError("codex CLI not found")

    result = run_judge("truth", DeadAdapter(), "PROMPT", make_cv(), PROFILE)
    assert result.outcome == OPERATIONAL_ABSTAIN
    assert "codex CLI not found" in result.note


# -- malformed grammar field types --------------------------------------------

@pytest.mark.parametrize("judge,bad", [
    # A nested list where fact ids belong.
    ("truth", {"element_id": BULLET_ID, "severity": "blocking",
               "quote": "Reduced onboarding time by 40%", "message": "m",
               "fact_ids": [[]]}),
    # A list where the second element id belongs.
    ("consistency", {"element_id": BULLET_ID, "severity": "blocking",
                     "quote": "Reduced onboarding time by 40%", "message": "m",
                     "second_element_id": ["summary"], "second_quote": "x"}),
    # A number where the quote belongs.
    ("writing", {"element_id": BULLET_ID, "severity": "blocking",
                 "quote": 42, "message": "m"}),
])
def test_malformed_grammar_field_types_are_schema_errors_never_exceptions(judge, bad):
    """Every model-returned field is type-checked before the grammar
    dereferences it: a malformed shape is a bounded schema retry ending in an
    operational abstention with a run record, never an escaping TypeError."""
    model = FakeJudgeModel([_output("FAIL", [bad])] * 3)
    result = run_judge(judge, model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == OPERATIONAL_ABSTAIN and result.attempts == 3
    assert model.calls == 3


@pytest.mark.parametrize("judge,bad", [
    ("truth", {"element_id": BULLET_ID, "severity": "blocking",
               "quote": "Reduced onboarding time by 40%", "message": "m",
               "fact_ids": [[]]}),
    ("consistency", {"element_id": BULLET_ID, "severity": "blocking",
                     "quote": "Reduced onboarding time by 40%", "message": "m",
                     "second_element_id": ["summary"], "second_quote": "x"}),
    ("writing", {"element_id": BULLET_ID, "severity": "blocking",
                 "quote": 42, "message": "m"}),
    ("writing", "not even an object"),
])
def test_the_validator_itself_drops_malformed_shapes_without_raising(judge, bad):
    valid, invalid = validate_findings(judge, [bad], make_cv(), PROFILE)
    assert valid == () and len(invalid) == 1


def test_a_validator_malfunction_is_an_operational_abstain_not_an_exception(monkeypatch):
    """No exception may escape a judge run: an escaping error would leave no
    run record and a dangling reservation."""
    import domain.gauntlet_judges as judges_module

    def boom(*_args, **_kwargs):
        raise TypeError("validator malfunction")

    monkeypatch.setattr(judges_module, "validate_findings", boom)
    model = FakeJudgeModel([_output("FAIL", [_finding()])])
    result = judges_module.run_judge("writing", model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == OPERATIONAL_ABSTAIN
    assert "validator malfunction" in result.note


# -- deterministic outcome mapping --------------------------------------------

def test_valid_blocking_finding_is_fail():
    model = FakeJudgeModel([_output("FAIL", [_finding()])])
    result = run_judge("writing", model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == FAIL and len(result.findings) == 1


def test_advisory_only_findings_record_pass_and_surface():
    model = FakeJudgeModel([_output("FAIL", [_finding(severity="advisory")])])
    result = run_judge("writing", model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == PASS
    assert result.findings[0].severity == "advisory"


def test_all_invalid_findings_is_operational_abstain_with_raw_preserved():
    model = FakeJudgeModel([_output("FAIL", [
        _finding(element_id="experiences[ghost].bullet[0]")])])
    result = run_judge("writing", model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == OPERATIONAL_ABSTAIN
    assert len(result.invalid_findings) == 1
    assert "ghost" in result.invalid_findings[0]


def test_model_change_between_retries_surfaces_as_mixed():
    model = FakeJudgeModel(["not json", _output()], model=["model-a", "model-b"])
    result = run_judge("writing", model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == PASS
    assert result.models == ("model-a", "model-b")
    assert result.model == "mixed"


def test_declared_terminal_abstain_stands():
    model = FakeJudgeModel([_output("TERMINAL_ABSTAIN")])
    result = run_judge("writing", model, "PROMPT", make_cv(), PROFILE)
    assert result.outcome == TERMINAL_ABSTAIN


# -- judge inputs -------------------------------------------------------------

def test_truth_payload_pairs_bullets_with_facts_and_attachments():
    payload = build_judge_payload("truth", make_cv(), make_snapshot())
    (entry,) = payload["entries"]
    assert entry["experience_id"] == "exp_1"
    (bullet,) = entry["bullets"]
    assert bullet["element_id"] == BULLET_ID
    (fact,) = bullet["source_facts"]
    assert fact["fact_id"] == "fact_1" and fact["statement"] == FACT
    assert fact["attached_experience_id"] == "exp_1"
    assert payload["summary"]["renderable_grounding_view"]["profile"] == PROFILE


def test_prompts_embed_the_payload_and_unknown_judge_is_refused():
    prompt = build_judge_prompt("consistency", "X {payload_json} Y",
                               make_cv(), make_snapshot())
    assert '"profile"' in prompt and prompt.startswith("X ")
    with pytest.raises(ValueError, match="unknown judge"):
        build_judge_payload("nonsense", make_cv(), make_snapshot())
