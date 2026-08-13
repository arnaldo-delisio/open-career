"""Headless Codex CLI ModelAdapter (OC-32 pattern, second model family per the
Gauntlet design's ratified decision 2: the Truth Judge runs independent of the
generator's model family). Shells out to `codex exec` non-interactively with
JSONL event output; the prompt travels over stdin, never as a shell argument,
and callers validate the payload against their own closed schema in code
(OC-5). Same validation and retry posture as the Claude Code adapter; an
outage surfaces as ModelUnavailableError / ModelCallError and the caller
records an OPERATIONAL_ABSTAIN.

Containment is a real filesystem boundary, not a CLI courtesy flag: the
invocation is wrapped in bubblewrap (`bwrap`), binding only the codex binary
and its runtime libraries (read-only), a minimal dedicated CODEX_HOME
holding just the auth material, and the per-call temp working directory. The
real HOME, the repository, and the instance directory are never mounted.
Network stays available (the model call needs it). Without bwrap the adapter
FAILS CLOSED unless the caller passes allow_uncontained=True explicitly;
nothing in the product sets it.

The staged CODEX_HOME is bound READ-WRITE, and must be: codex initializes an
in-process app-server that writes there, and a read-only bind fails the call
outright ("failed to initialize in-process app-server client: Read-only file
system"). This does not widen exposure. That directory is a throwaway copy
created per call under a temp dir and destroyed with it, holding only the
copied auth material; the real CODEX_HOME is never mounted, so nothing the
sandboxed process writes can reach it or outlive the call.

SECURITY NOTE, stated precisely: inside the sandbox the agent process can
still read its own auth.json (any CLI adapter can; a process can always read
its own credentials) and it has network, so a prompt injection could
exfiltrate the auth token. Today this residual risk is acceptable because
the Gauntlet's judge inputs are built exclusively from user-approved career
state (persisted context snapshots and content models); no third-party or
posting-derived text exists in the system yet. TRIPWIRE: before any
posting-derived or otherwise third-party text enters judge inputs (the
discovery phase), this adapter must gain credential mediation (a broker that
keeps the token out of the sandboxed process) or be replaced by a
non-agentic completion endpoint; shipping discovery-fed judges over this
adapter as-is would hand injected text a readable token and a network."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from adapters.models.claude_code import ModelCallError, _cli_version
from domain.ports import ModelAdapter, ModelUnavailableError

# codex's own agentic-shell sandbox stays pinned read-only as defense in
# depth inside the outer filesystem boundary.
_EXEC_ARGS = ("exec", "--sandbox", "read-only", "--skip-git-repo-check",
              "--ephemeral", "--json", "-")

# Auth material codex needs; nothing else from the real CODEX_HOME travels.
_AUTH_FILES = ("auth.json",)

# System paths bound read-only for the binary's runtime deps and TLS/DNS.
_SYSTEM_RO_BINDS = ("/usr", "/lib", "/lib64", "/bin", "/etc/ssl",
                    "/etc/ca-certificates", "/etc/resolv.conf",
                    "/etc/nsswitch.conf", "/etc/hosts")

_SANDBOX_HOME = "/codex-home"
_SANDBOX_WORK = "/work"


class CodexCliAdapter(ModelAdapter):
    def __init__(self, command: str = "codex", timeout_seconds: int = 600,
                 run=None, which=None, codex_home: str | None = None,
                 allow_uncontained: bool = False):
        self._command = command
        self._timeout_seconds = timeout_seconds
        # Injectable for tests (never call real CLIs or sandboxes in the suite).
        self._run = run
        self._which = which or shutil.which
        self._codex_home = codex_home
        self._allow_uncontained = allow_uncontained
        self._provider_version: str | None = None

    def provider_version(self) -> str:
        """`codex --version` verbatim (e.g. 'codex-cli 0.145.0'), cached per
        adapter instance. This is the ONLY identity this backend exposes: the
        `codex exec --json` event stream carries no resolved model field, so
        model identity is honestly 'unreported' and the provider version is
        what bounds a demonstrated claim."""
        if self._provider_version is None:
            path = self._which(self._command)
            self._provider_version = (
                _cli_version(self._run or subprocess.run, path)
                if path else "unavailable")
        return self._provider_version

    def complete(self, prompt: str) -> str:
        return self._complete_events(prompt)[0]

    def complete_with_meta(self, prompt: str) -> tuple[str, dict]:
        """Model identity observed from the event stream when the CLI reports
        one; unreported otherwise (the Gauntlet's model-identity rule)."""
        return self._complete_events(prompt)

    # -- containment -------------------------------------------------------

    def _real_codex_home(self) -> Path:
        if self._codex_home:
            return Path(self._codex_home)
        env_home = os.environ.get("CODEX_HOME")
        if env_home:
            return Path(env_home)
        return Path(os.environ.get("HOME", "/nonexistent")) / ".codex"

    def _make_minimal_home(self, staging: Path) -> Path:
        """A dedicated CODEX_HOME with only the auth material, so even a
        successful escape inside the sandbox finds nothing else."""
        minimal = staging / "codex-home"
        minimal.mkdir()
        real = self._real_codex_home()
        for name in _AUTH_FILES:
            source = real / name
            if source.is_file():
                shutil.copyfile(source, minimal / name)
        return minimal

    def _build_invocation(self, workdir: Path, minimal_home: Path,
                          codex_path: str) -> tuple[list[str], dict, str]:
        """(argv, env, containment) for one call; fails closed when no
        sandbox tool exists and containment was not explicitly waived."""
        bwrap = self._which("bwrap")
        if bwrap:
            argv = [bwrap, "--unshare-all", "--share-net", "--die-with-parent",
                    "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
            for path in _SYSTEM_RO_BINDS:
                if os.path.exists(path):
                    argv += ["--ro-bind", path, path]
            argv += ["--bind", str(minimal_home), _SANDBOX_HOME,
                     "--bind", str(workdir), _SANDBOX_WORK,
                     "--chdir", _SANDBOX_WORK,
                     codex_path, *_EXEC_ARGS]
            env = {"PATH": "/usr/bin:/bin", "HOME": _SANDBOX_WORK,
                   "CODEX_HOME": _SANDBOX_HOME, "TERM": "dumb"}
            return argv, env, "bwrap"
        if self._allow_uncontained:
            env = {k: os.environ[k] for k in
                   ("PATH", "HOME", "CODEX_HOME", "TERM", "LANG", "LC_ALL")
                   if k in os.environ}
            return [codex_path, *_EXEC_ARGS], env, "uncontained"
        raise ModelUnavailableError(
            "bubblewrap ('bwrap') not found; the Codex judge call must run"
            " inside a filesystem sandbox. Install bubblewrap, or construct"
            " CodexCliAdapter(allow_uncontained=True) explicitly to waive"
            " containment (the product never does).")

    # -- the call ----------------------------------------------------------

    def _complete_events(self, prompt: str) -> tuple[str, dict]:
        run = self._run or subprocess.run
        codex_path = self._which(self._command)
        if not codex_path:
            raise ModelUnavailableError(
                f"'{self._command}' CLI not found on PATH; the Truth Judge runs"
                " through the Codex CLI (Gauntlet design, ratified decision 2)."
                " Install the codex CLI and sign in, or configure a different"
                " ModelAdapter.")
        try:
            with tempfile.TemporaryDirectory(prefix="codex-judge-") as staging_dir:
                staging = Path(staging_dir)
                workdir = staging / "work"
                workdir.mkdir()
                minimal_home = self._make_minimal_home(staging)
                argv, env, _containment = self._build_invocation(
                    workdir, minimal_home, codex_path)
                proc = run(
                    argv,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    cwd=str(workdir),
                    env=env,
                )
        except FileNotFoundError as e:
            raise ModelUnavailableError(
                f"sandbox or codex binary could not be executed: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise ModelCallError(
                f"'{self._command}' timed out after {self._timeout_seconds}s") from e
        except OSError as e:  # PermissionError and other spawn failures
            raise ModelCallError(f"'{self._command}' could not run: {e}") from e
        if proc.returncode != 0:
            raise ModelCallError(
                f"'{self._command}' exited {proc.returncode}:"
                f" {proc.stderr.strip() or proc.stdout.strip()}")
        return _parse_jsonl_events(self._command, proc.stdout)


def _parse_jsonl_events(command: str, stdout: str) -> tuple[str, dict]:
    """The last agent message in the event stream is the completion; the
    model identity is taken from any event that reports one. Both current
    (`item.completed` with an `agent_message` item) and legacy (`msg` with
    type `agent_message`) event shapes are read."""
    result: str | None = None
    model: str | None = None
    saw_event = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # the CLI may interleave plain-text status lines
        if not isinstance(event, dict):
            continue
        saw_event = True
        model = _reported_model(event) or model
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" \
                and isinstance(item.get("text"), str):
            result = item["text"]
        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message" \
                and isinstance(msg.get("message"), str):
            result = msg["message"]
    if not saw_event:
        raise ModelCallError(f"'{command}' emitted no parseable JSON events")
    if result is None:
        raise ModelCallError(f"'{command}' event stream carried no agent message")
    return result, {"model": model or "unreported"}


def _reported_model(event: dict) -> str | None:
    for container in (event, event.get("msg"), event.get("config"),
                      event.get("session")):
        if isinstance(container, dict):
            model = container.get("model")
            if isinstance(model, str) and model:
                return model
    return None
