"""Headless Claude Code ModelAdapter (OC-32): shells out to `claude -p`,
subscription-backed, no API key. The prompt travels over stdin, never as a
shell argument; output arrives as the CLI's JSON envelope and callers validate
the payload against their own closed schema in code (OC-5)."""

import json
import subprocess

from domain.ports import ModelAdapter, ModelUnavailableError


class ModelCallError(RuntimeError):
    """One model call failed operationally (timeout, crash, malformed
    envelope). The backend itself may still be usable; callers can degrade
    rather than die."""


class ClaudeCodeAdapter(ModelAdapter):
    def __init__(self, command: str = "claude", timeout_seconds: int = 600, run=None):
        self._command = command
        self._timeout_seconds = timeout_seconds
        # Injectable for tests (never call the real CLI in the suite); resolved
        # at call time so monkeypatching subprocess.run also works.
        self._run = run

    def complete(self, prompt: str) -> str:
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
            raise ModelCallError(
                f"'{self._command}' exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ModelCallError(f"'{self._command}' emitted invalid JSON envelope: {e}") from e
        if not isinstance(envelope, dict) or envelope.get("is_error") or "result" not in envelope:
            raise ModelCallError(f"'{self._command}' reported an error: {envelope}")
        if not isinstance(envelope["result"], str):
            raise ModelCallError(
                f"'{self._command}' result field is {type(envelope['result']).__name__}, not text")
        return envelope["result"]
