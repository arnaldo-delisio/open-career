"""Extraction: closed-schema validation in code, one retry, and the
ClaudeCodeAdapter subprocess boundary (faked; the suite never calls the real CLI)."""

import json
import subprocess

import pytest

from adapters.models.claude_code import ClaudeCodeAdapter, ModelCallError
from domain.extraction import CvExtractionService, ExtractionError, parse_extraction
from domain.ports import ModelAdapter, ModelUnavailableError

VALID = json.dumps({
    "experiences": [{"kind": "role", "title": "Backend Engineer", "org": "Acme",
                     "start_date": "2021", "end_date": "2023", "summary": None}],
    "facts": [{"experience_index": 0, "fact_type": "achievement",
               "statement": "Built the order service", "source_location": "Experience section"}],
})


class ScriptedModel(ModelAdapter):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def test_parse_extraction_accepts_valid_output():
    extraction = parse_extraction(VALID)
    assert extraction.experiences[0].title == "Backend Engineer"
    assert extraction.facts[0].experience_index == 0


def test_parse_extraction_strips_code_fences():
    assert parse_extraction(f"```json\n{VALID}\n```").facts[0].fact_type == "achievement"


@pytest.mark.parametrize("bad,message", [
    ("not json", "not valid JSON"),
    ('{"experiences": []}', "exactly 'experiences' and 'facts'"),
    (json.dumps({"experiences": [], "facts": [], "profile": {}}), "exactly 'experiences' and 'facts'"),
    (json.dumps({"experiences": [{"kind": "wizard", "title": "x"}], "facts": []}), "'kind' must be one of"),
    (json.dumps({"experiences": [{"kind": "role", "title": "x", "salary": "100k"}], "facts": []}),
     "allowed keys"),
    (json.dumps({"experiences": [], "facts": [{"fact_type": "achievement", "statement": "s",
                                               "experience_index": 3}]}),
     "valid experiences index"),
    (json.dumps({"experiences": [], "facts": [{"fact_type": "achievement", "statement": ""}]}),
     "non-empty string"),
])
def test_parse_extraction_rejects_schema_deviations(bad, message):
    """The schema is closed: extra keys (a channel for model-decided values,
    OC-5) and malformed entries are rejected in code."""
    with pytest.raises(ExtractionError, match=message):
        parse_extraction(bad)


def test_service_retries_once_on_validation_failure():
    model = ScriptedModel(["garbage", VALID])
    extraction = CvExtractionService(model, "Extract.\n{cv_text}").extract("CV TEXT")
    assert len(model.prompts) == 2
    assert "failed schema validation" in model.prompts[1]
    assert extraction.facts[0].statement == "Built the order service"


def test_service_fails_after_second_invalid_output():
    model = ScriptedModel(["garbage", "still garbage"])
    with pytest.raises(ExtractionError, match="failed validation twice"):
        CvExtractionService(model, "Extract.\n{cv_text}").extract("CV TEXT")
    assert len(model.prompts) == 2  # exactly one retry


def test_service_substitutes_cv_text():
    model = ScriptedModel([VALID])
    CvExtractionService(model, "Extract from:\n{cv_text}").extract("MY CV")
    assert "MY CV" in model.prompts[0]


def _fake_run(stdout="", returncode=0, raises=None):
    calls = []

    def run(argv, input=None, capture_output=None, text=None, timeout=None):
        calls.append({"argv": argv, "input": input})
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="boom")

    run.calls = calls
    return run


def test_adapter_sends_prompt_on_stdin_and_unwraps_result():
    run = _fake_run(stdout=json.dumps({"is_error": False, "result": "PAYLOAD"}))
    adapter = ClaudeCodeAdapter(run=run)
    assert adapter.complete("the prompt") == "PAYLOAD"
    call = run.calls[0]
    assert call["argv"] == ["claude", "-p", "--output-format", "json"]
    assert call["input"] == "the prompt"  # stdin, never a shell argument


def test_adapter_missing_cli_is_a_clear_error():
    run = _fake_run(raises=FileNotFoundError("claude"))
    with pytest.raises(ModelUnavailableError, match="'claude' CLI not found on PATH"):
        ClaudeCodeAdapter(run=run).complete("p")


def test_adapter_nonzero_exit_is_a_model_call_error():
    run = _fake_run(returncode=2)
    with pytest.raises(ModelCallError, match="exited 2"):
        ClaudeCodeAdapter(run=run).complete("p")


def test_adapter_error_envelope_is_a_model_call_error():
    run = _fake_run(stdout=json.dumps({"is_error": True, "result": "overloaded"}))
    with pytest.raises(ModelCallError, match="reported an error"):
        ClaudeCodeAdapter(run=run).complete("p")


def test_adapter_timeout_is_a_model_call_error():
    run = _fake_run(raises=subprocess.TimeoutExpired(cmd="claude", timeout=600))
    with pytest.raises(ModelCallError, match="timed out after 600s"):
        ClaudeCodeAdapter(run=run).complete("p")


def test_adapter_permission_error_is_a_model_call_error():
    run = _fake_run(raises=PermissionError("denied"))
    with pytest.raises(ModelCallError, match="could not run"):
        ClaudeCodeAdapter(run=run).complete("p")


def test_adapter_non_string_result_is_a_model_call_error():
    run = _fake_run(stdout=json.dumps({"result": ["a", "list"]}))
    with pytest.raises(ModelCallError, match="not text"):
        ClaudeCodeAdapter(run=run).complete("p")
