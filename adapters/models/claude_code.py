"""Headless Claude Code ModelAdapter (OC-32): shells out to `claude -p`,
subscription-backed, no API key. The prompt travels over stdin, never as a
shell argument; output arrives as the CLI's JSON envelope and callers validate
the payload against their own closed schema in code (OC-5)."""

import json
import subprocess

from domain.ports import ModelAdapter, ModelUnavailableError


# The wall-clock bound one model call is held to. The discovery lease
# relationship (workers/discovery/run.py) is computed from this number, so it
# is a constant here rather than a caller's choice.
DEFAULT_TIMEOUT_SECONDS = 600


class ModelCallError(RuntimeError):
    """One model call failed operationally (timeout, crash, malformed
    envelope). The backend itself may still be usable; callers can degrade
    rather than die.

    provider_status carries the provider's own HTTP status when the envelope
    reported one, so a caller can persist a diagnostic without inspecting the
    message (which may quote model output)."""

    def __init__(self, message: str, provider_status: int | None = None):
        super().__init__(message)
        self.provider_status = provider_status


# Provider statuses that describe the BACKEND, never the row being worked: the
# subscription quota is spent (429), the credentials are rejected (401), the
# account is not permitted (403), or the provider itself failed (any 5xx). No
# posting and no retry can fix any of them, so iterating rows against one only
# burns the run's budget on an identical unrecoverable error (observed live:
# ten extraction calls in ~50s, ten rows marked failed for something that was
# never their fault).
BACKEND_UNAVAILABLE_STATUSES = frozenset({401, 403, 429})


def is_backend_unavailable(status: int | None) -> bool:
    return status is not None and (status in BACKEND_UNAVAILABLE_STATUSES
                                   or 500 <= status <= 599)


class ClaudeCodeAdapter(ModelAdapter):
    def __init__(self, command: str = "claude",
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, run=None):
        self._command = command
        self._timeout_seconds = timeout_seconds
        # Injectable for tests (never call the real CLI in the suite); resolved
        # at call time so monkeypatching subprocess.run also works.
        self._run = run
        self._provider_version: str | None = None

    @property
    def call_timeout_s(self) -> int:
        """The wall-clock bound one call is held to. Read by callers whose own
        timing depends on it (the discovery lease relationship), so an
        injected non-default timeout cannot silently invalidate it."""
        return self._timeout_seconds

    def complete(self, prompt: str) -> str:
        return self._complete_envelope(prompt)["result"]

    def complete_with_meta(self, prompt: str) -> tuple[str, dict]:
        """The envelope's provider-reported resolved model, observed per call,
        never asserted from config (the Gauntlet design's model-identity
        rule); absent field reports honestly as unreported.

        Observed fact, not an assumption: today's `claude -p --output-format
        json` envelope carries no `model` or `modelName` key at all (its keys
        are result, usage, modelUsage, session_id, cost and timing fields), so
        this adapter reports 'unreported' in practice, exactly like the Codex
        one. The lookup stays because the field is the provider's to add;
        nothing is inferred from modelUsage, which was empty when observed."""
        envelope = self._complete_envelope(prompt)
        model = envelope.get("model") or envelope.get("modelName")
        return envelope["result"], {"model": model if isinstance(model, str) and model
                                    else "unreported"}

    def provider_version(self) -> str:
        """`claude --version` verbatim (e.g. '2.1.231 (Claude Code)'), cached
        per adapter instance. Never inferred: an unusable CLI reports
        'unavailable'."""
        if self._provider_version is None:
            self._provider_version = _cli_version(
                self._run or subprocess.run, self._command)
        return self._provider_version

    def _backend_unavailable_error(self, envelope) -> ModelUnavailableError | None:
        """The one classification both exit paths share: a backend condition in
        the envelope becomes ModelUnavailableError, anything else stays the
        caller's per-row error. The message names the status only: the
        envelope's `result` field is provider prose and never travels into a
        persisted record."""
        status = _provider_status(envelope)
        if not is_backend_unavailable(status):
            return None
        error = ModelUnavailableError(
            f"'{self._command}' reported provider status {status}"
            " (subscription limit, authentication, or permission);"
            " the backend is unavailable, not this input")
        error.provider_status = status
        return error

    def _complete_envelope(self, prompt: str) -> dict:
        run = self._run or subprocess.run
        try:
            proc = run(
                [self._command, "-p", "--output-format", "json"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as e:
            raise ModelUnavailableError(
                f"'{self._command}' CLI not found on PATH; extraction runs through headless"
                " Claude Code (OC-32). Install Claude Code and sign in, or configure a"
                " different ModelAdapter."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ModelCallError(
                f"'{self._command}' timed out after {self._timeout_seconds}s") from e
        except OSError as e:  # PermissionError and other spawn failures
            raise ModelCallError(f"'{self._command}' could not run: {e}") from e
        if proc.returncode != 0:
            # A spent quota exits NON-zero with a well-formed envelope (observed
            # live: rc=1, "terminal_reason":"api_error", "api_error_status":429),
            # so the envelope is classified BEFORE the exit code is reported.
            # Otherwise an unavailable backend reads as a bad row and burns the
            # budget and the queue's attempts, exactly what
            # BACKEND_UNAVAILABLE_STATUSES exists to prevent.
            try:
                envelope = json.loads(proc.stdout)
            except (json.JSONDecodeError, TypeError):
                envelope = None
            error = self._backend_unavailable_error(envelope)
            if error is not None:
                raise error
            raise ModelCallError(
                f"'{self._command}' exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ModelCallError(f"'{self._command}' emitted invalid JSON envelope: {e}") from e
        if not isinstance(envelope, dict) or envelope.get("is_error") or "result" not in envelope:
            status = _provider_status(envelope)
            error = self._backend_unavailable_error(envelope)
            if error is not None:
                raise error
            raise ModelCallError(
                f"'{self._command}' reported an error: {envelope}",
                provider_status=status)
        if not isinstance(envelope["result"], str):
            raise ModelCallError(
                f"'{self._command}' result field is {type(envelope['result']).__name__}, not text")
        return envelope


def _provider_status(envelope) -> int | None:
    """The provider's own HTTP status from the CLI envelope, when it reports
    one (`api_error_status`, observed alongside "terminal_reason":"api_error").
    Absent, non-integer, or outside the HTTP status range reads as unknown,
    never guessed: the envelope is the CLI's to write, and callers persist this
    number, so it is range-bounded here rather than trusted."""
    if not isinstance(envelope, dict):
        return None
    status = envelope.get("api_error_status")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    return status if 100 <= status <= 599 else None


def _cli_version(run, command: str) -> str:
    """One `<command> --version` call, verbatim output. Any failure is
    'unavailable': a provider version is observed or absent, never guessed."""
    try:
        proc = run([command, "--version"], capture_output=True, text=True,
                   timeout=30)
    except Exception:
        return "unavailable"
    if getattr(proc, "returncode", 1) != 0:
        return "unavailable"
    version = (proc.stdout or "").strip().splitlines()
    return version[0].strip() if version and version[0].strip() else "unavailable"
