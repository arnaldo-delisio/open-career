"""Model adapter tests (spec: the scope's decisions/gauntlet-design.md,
"Judge mechanics", model identity): complete_with_meta as a concrete base
default keeping every complete-only double instantiable, the Claude adapter's
envelope model field, and the new Codex CLI adapter with an injectable run
(the suite never calls a real CLI)."""

import json
import subprocess
import tempfile

import pytest

from adapters.models.claude_code import ClaudeCodeAdapter, ModelCallError
from adapters.models.codex_cli import CodexCliAdapter
from domain.ports import ModelAdapter, ModelUnavailableError


class CompleteOnlyDouble(ModelAdapter):
    """The regression contract: existing doubles implement only complete()
    and must stay instantiable unchanged (the port gained a concrete default,
    never a second abstract method)."""

    def complete(self, prompt: str) -> str:
        return "text"


def test_complete_only_doubles_stay_instantiable_with_unreported_metadata():
    double = CompleteOnlyDouble()
    assert double.complete_with_meta("p") == ("text", {"model": "unreported"})


class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_claude_adapter_reads_the_envelope_model_field():
    envelope = json.dumps({"result": "hello", "model": "claude-x-1"})
    adapter = ClaudeCodeAdapter(run=lambda *a, **k: FakeProc(envelope))
    assert adapter.complete("p") == "hello"
    assert adapter.complete_with_meta("p") == ("hello", {"model": "claude-x-1"})


def test_claude_adapter_reports_unreported_when_the_field_is_absent():
    envelope = json.dumps({"result": "hello"})
    adapter = ClaudeCodeAdapter(run=lambda *a, **k: FakeProc(envelope))
    assert adapter.complete_with_meta("p") == ("hello", {"model": "unreported"})


def _which_with(*tools):
    """An injectable which: codex always resolves; only the named sandbox
    tools do."""
    available = {"codex", *tools}
    return lambda name: f"/usr/bin/{name}" if name in available else None


def _adapter(run, tools=("bwrap",), tmp_path=None, **kwargs):
    codex_home = None
    if tmp_path is not None:
        codex_home = tmp_path / "real-codex-home"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "auth.json").write_text('{"token": "t"}')
    return CodexCliAdapter(run=run, which=_which_with(*tools),
                           codex_home=str(codex_home) if codex_home else "/nonexistent",
                           **kwargs)


def test_codex_adapter_parses_jsonl_events_and_observed_model():
    lines = "\n".join([
        json.dumps({"type": "session.created", "model": "gpt-5-codex"}),
        "plain status noise",
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "first"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "final answer"}}),
    ])
    calls = {}

    def run(argv, **kwargs):
        calls["argv"] = argv
        calls["input"] = kwargs["input"]
        return FakeProc(lines)

    text, meta = _adapter(run).complete_with_meta("the prompt")
    assert text == "final answer" and meta == {"model": "gpt-5-codex"}
    # Headless non-interactive exec with JSON output; prompt over stdin,
    # never a shell argument.
    argv = calls["argv"]
    codex_at = argv.index("/usr/bin/codex")
    assert argv[codex_at:codex_at + 2] == ["/usr/bin/codex", "exec"]
    assert argv[-2:] == ["--json", "-"]
    assert calls["input"] == "the prompt"


def test_codex_adapter_bwrap_containment_argv_shape(tmp_path):
    """The filesystem boundary: bwrap wraps the call, binding only system
    runtime paths (ro), the staged throwaway CODEX_HOME (rw, since codex
    writes there), and the per-call temp workdir (rw); the real HOME, repo,
    and instance are never mounted and the child env points inside the
    sandbox."""
    import os

    calls = {}

    def run(argv, **kwargs):
        calls["argv"] = argv
        calls["env"] = kwargs["env"]
        calls["cwd"] = kwargs["cwd"]
        return FakeProc(json.dumps({"type": "item.completed",
                                    "item": {"type": "agent_message",
                                             "text": "ok"}}))

    _adapter(run, tmp_path=tmp_path).complete("p")
    argv = calls["argv"]
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in argv and "--share-net" in argv  # network kept
    # The staged CODEX_HOME is bound read-write (codex initializes an
    # in-process app-server that writes there; a read-only bind fails the
    # call). Containment holds because it is a per-call throwaway copy under
    # a temp dir holding only the auth material.
    home_bind = argv.index("/codex-home")
    assert argv[home_bind - 2] == "--bind"
    minimal_home = argv[home_bind - 1]
    assert "codex-judge-" in minimal_home
    assert minimal_home.startswith(tempfile.gettempdir())
    # Every system bind stays read-only: only the staged home and the
    # per-call workdir are writable.
    work_bind = argv.index("/work")
    assert argv[work_bind - 2] == "--bind"
    writable = [argv[i + 2] for i, a in enumerate(argv) if a == "--bind"]
    assert writable == ["/codex-home", "/work"]
    # The real HOME is never mounted.
    real_home = os.environ.get("HOME")
    assert real_home and real_home not in argv
    assert str(tmp_path / "real-codex-home") not in argv
    # codex's own read-only shell sandbox stays pinned inside the boundary.
    codex_at = argv.index("/usr/bin/codex")
    assert argv[codex_at + 1:codex_at + 4] == ["exec", "--sandbox", "read-only"]
    # The child env points inside the sandbox, never at the real HOME.
    assert calls["env"]["HOME"] == "/work"
    assert calls["env"]["CODEX_HOME"] == "/codex-home"
    assert set(calls["env"]) == {"PATH", "HOME", "CODEX_HOME", "TERM"}


def test_codex_adapter_fails_closed_without_bwrap(tmp_path):
    """bubblewrap or nothing: firejail is not a fallback (removed by review),
    and an absent bwrap fails closed rather than running uncontained."""
    def run(*a, **k):
        raise AssertionError("nothing must run uncontained")

    with pytest.raises(ModelUnavailableError, match="bubblewrap"):
        _adapter(run, tools=(), tmp_path=tmp_path).complete("p")
    # firejail on the box changes nothing: still fail closed.
    with pytest.raises(ModelUnavailableError, match="bubblewrap"):
        _adapter(run, tools=("firejail",), tmp_path=tmp_path).complete("p")
    # The explicit waiver (which nothing in the product sets) still runs,
    # bare codex argv, scrubbed env.
    calls = {}

    def run_ok(argv, **kwargs):
        calls["argv"] = argv
        return FakeProc(json.dumps({"type": "item.completed",
                                    "item": {"type": "agent_message",
                                             "text": "ok"}}))

    _adapter(run_ok, tools=(), tmp_path=tmp_path,
             allow_uncontained=True).complete("p")
    assert calls["argv"][0] == "/usr/bin/codex"


def test_product_wiring_never_waives_containment():
    from apps.cli.main import _judge_models
    import inspect

    # The default constructed by the CLI keeps allow_uncontained False.
    assert inspect.signature(CodexCliAdapter.__init__).parameters[
        "allow_uncontained"].default is False
    source = inspect.getsource(_judge_models)
    assert "allow_uncontained" not in source


def test_codex_adapter_reads_the_legacy_msg_event_shape():
    lines = json.dumps({"msg": {"type": "agent_message", "message": "hi",
                                "model": "o4-codex"}})
    adapter = _adapter(lambda *a, **k: FakeProc(lines))
    assert adapter.complete_with_meta("p") == ("hi", {"model": "o4-codex"})


def test_codex_adapter_error_posture_matches_the_claude_adapter():
    with pytest.raises(ModelUnavailableError, match="not found on PATH"):
        CodexCliAdapter(run=lambda *a, **k: FakeProc(""),
                        which=lambda _n: None).complete("p")

    def missing(*a, **k):
        raise FileNotFoundError("codex")

    with pytest.raises(ModelUnavailableError, match="could not be executed"):
        _adapter(missing).complete("p")

    with pytest.raises(ModelCallError, match="exited 2"):
        _adapter(lambda *a, **k: FakeProc("", returncode=2,
                                          stderr="boom")).complete("p")

    with pytest.raises(ModelCallError, match="no parseable JSON"):
        _adapter(lambda *a, **k: FakeProc("nonsense")).complete("p")

    with pytest.raises(ModelCallError, match="no agent message"):
        _adapter(lambda *a, **k: FakeProc(
            json.dumps({"type": "turn.completed"}))).complete("p")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=600)

    with pytest.raises(ModelCallError, match="timed out"):
        _adapter(timeout).complete("p")


# -- provider identity --------------------------------------------------------

def test_provider_version_is_observed_verbatim_and_cached(tmp_path):
    """Provider identity is what bounds a claim when the backend reports no
    model: the CLI's own version string, verbatim, asked once."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return FakeProc("codex-cli 0.145.0\n")

    adapter = _adapter(run, tmp_path=tmp_path)
    assert adapter.provider_version() == "codex-cli 0.145.0"
    assert adapter.provider_version() == "codex-cli 0.145.0"  # cached
    assert calls == [["/usr/bin/codex", "--version"]]

    claude_calls = []

    def claude_run(argv, **kwargs):
        claude_calls.append(argv)
        return FakeProc("2.1.231 (Claude Code)\n")

    claude = ClaudeCodeAdapter(run=claude_run)
    assert claude.provider_version() == "2.1.231 (Claude Code)"
    assert claude_calls == [["claude", "--version"]]


def test_an_unusable_cli_reports_unavailable_never_a_guess(tmp_path):
    def failing(argv, **kwargs):
        raise OSError("no such binary")

    assert ClaudeCodeAdapter(run=failing).provider_version() == "unavailable"

    def nonzero(argv, **kwargs):
        return FakeProc("", returncode=1)

    assert ClaudeCodeAdapter(run=nonzero).provider_version() == "unavailable"
    # No codex binary on PATH: nothing to ask.
    assert _adapter(failing, tools=("bwrap",),
                    tmp_path=tmp_path).provider_version() == "unavailable"


def test_the_base_adapter_reports_no_provider_version():
    """A backend that cannot be asked never fabricates one."""
    class Bare(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return "x"

    assert Bare().provider_version() == "unavailable"
    assert Bare().complete_with_meta("p") == ("x", {"model": "unreported"})


# -- backend unavailable vs. one bad row --------------------------------------

def _quota_envelope(status: int) -> str:
    """The live envelope shape observed when the operator's subscription limit
    was hit: rc=0, is_error true, the provider's status, prose in result."""
    return json.dumps({
        "type": "result", "subtype": "error_during_execution",
        "is_error": True, "terminal_reason": "api_error",
        "api_error_status": status,
        "result": "You've reached your Fable 5 limit...",
    })


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 529])
def test_backend_status_envelopes_raise_model_unavailable(status):
    """429 (quota), 401 (auth), 403 (permission) and any 5xx (the provider
    itself failed) describe the backend: no row can fix them, so they must not
    be charged to the row being worked (5xx added, Codex round 2)."""
    adapter = ClaudeCodeAdapter(
        run=lambda *a, **k: FakeProc(_quota_envelope(status)))
    with pytest.raises(ModelUnavailableError) as excinfo:
        adapter.complete("p")
    assert excinfo.value.provider_status == status
    # The provider's prose never travels in our message.
    assert "Fable 5 limit" not in str(excinfo.value)


def test_a_row_shaped_envelope_error_stays_a_per_row_model_call_error():
    envelope = json.dumps({"is_error": True, "result": "garbled"})
    adapter = ClaudeCodeAdapter(run=lambda *a, **k: FakeProc(envelope))
    with pytest.raises(ModelCallError) as excinfo:
        adapter.complete("p")
    assert excinfo.value.provider_status is None

    # A status that is not a backend condition is per-row too, and is carried.
    adapter = ClaudeCodeAdapter(
        run=lambda *a, **k: FakeProc(_quota_envelope(400)))
    with pytest.raises(ModelCallError) as excinfo:
        adapter.complete("p")
    assert excinfo.value.provider_status == 400


def test_an_out_of_range_provider_status_is_unknown_not_persisted():
    """The envelope is the CLI's to write and callers persist the status, so
    it is range-bounded here: a hostile or malformed oversized value reads as
    unknown and cannot inflate a stored diagnostic (Codex round 1)."""
    for status in (10 ** 40, -1, 99, 600, "429", True):
        envelope = json.dumps({"is_error": True, "result": "x",
                               "api_error_status": status})
        adapter = ClaudeCodeAdapter(run=lambda *a, **k: FakeProc(envelope))
        with pytest.raises(ModelCallError) as excinfo:
            adapter.complete("p")
        assert excinfo.value.provider_status is None

    # Bounded at the persistence boundary too, not only in the envelope
    # parser: a caller constructing the error directly cannot widen it.
    from workers.discovery.run import _diagnostic
    assert _diagnostic(ModelCallError("x")) == "[ModelCallError]"
    for bad in (10 ** 40, -1, 99, 600, True, "429"):
        assert _diagnostic(ModelCallError("x", provider_status=bad)) == \
            "[ModelCallError]"
    assert _diagnostic(ModelCallError("x", provider_status=429)) == \
        "[ModelCallError, provider status 429]"
