"""The Gauntlet run: stage zero, then the three judges, then the verdict
(spec: the scope's decisions/gauntlet-design.md, "Verdict, report, and
lifecycle").

A run is reads plus one append: no lifecycle state is minted on the version.
Admission is a fenced reservation (atomic claim, concurrent heartbeat renewal
on its own repository connection, conditional consume in the run-insert
transaction), so two concurrent invocations cannot collide on one attempt and
an expired worker can never establish the effective adjudication. The runs
table is append-only and write-once; re-running a suite is permitted only
while no complete attempt exists for it.

suite_version is code identity only, knowable before any call; model identity
is observed per call and recorded, never pre-stamped."""

import hashlib
import json
import re
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Mapping

from domain.cv_model import CvModelError, parse_cv_model
from domain.gauntlet_invariants import (
    InvariantResult,
    PolicySnapshotError,
    build_policy_snapshot,
    invariants_passed,
    parse_policy_snapshot,
    policy_snapshot_json,
    run_invariants,
)
from domain.gauntlet_judges import (
    FAIL,
    JUDGES,
    NOT_RUN,
    OPERATIONAL_ABSTAIN,
    PASS,
    TERMINAL_ABSTAIN,
    JudgeResult,
    build_judge_prompt,
    not_run,
    run_judge,
)
from domain.ids import new_id
from domain.packages import PackageVersion
from domain.ports import PackageRepository, PdfTextExtractor, StorageAdapter

# Code identity of the whole suite: invariant rules, judge set, prompt
# versions, schema versions. Any change to any of them bumps this string.
# gauntlet-2: the work-authorization detector moved from bare-token substrings
# to employment-authorization phrase patterns (projection version wa-2).
# gauntlet-3: the Codex judge invocation changed (the staged CODEX_HOME is
# bound read-write, without which the call fails), and run identity now
# records the observed provider version beside the observed model identity.
SUITE_VERSION = "gauntlet-3"

RESERVATION_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 10

# Run verdicts.
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_ATTENTION = "ATTENTION"
VERDICT_INCOMPLETE = "INCOMPLETE"  # recorded on an incomplete attempt only


class ReservationLostError(RuntimeError):
    """The worker no longer holds the reservation (expired or taken over);
    its result is discarded entirely and nothing is inserted."""


@dataclass(frozen=True)
class GauntletRun:
    """One append-only run row. seq is the sole ordering authority; attempt
    is a display label."""

    seq: int
    id: str
    package_version_id: str
    suite_version: str
    attempt: int
    complete: int
    report_json: str
    prompt_inputs_locator: str
    prompt_inputs_hash: str
    raw_completions_locator: str
    raw_completions_hash: str
    resolved_models_json: str
    policy_snapshot_locator: str
    policy_snapshot_hash: str
    created_at: str | None = None

    @property
    def verdict(self) -> str:
        return json.loads(self.report_json)["verdict"]


@dataclass(frozen=True)
class GauntletRunResult:
    run: GauntletRun | None
    verdict: str | None
    detail: str


def gauntlet_object_locator(run_id: str, name: str) -> str:
    """Run-derived, write-once object locator (run ids are unique, so a stale
    worker's objects are inert and attributable, never referenced by a row)."""
    return f"gauntlet/{run_id}/{name}"


def reduce_verdict(invariants: tuple[InvariantResult, ...],
                   judges: tuple[JudgeResult, ...]) -> tuple[str, bool, str]:
    """(verdict, complete, stop_reason), in the design's precedence order.
    A valid FAIL is terminal even beside an operational abstention; an
    operational abstention otherwise leaves the attempt incomplete; an
    attention invariant, a terminal abstention, or any advisory finding caps
    at ATTENTION; otherwise PASS."""
    if not invariants_passed(invariants):
        failed = [r.rule for r in invariants if r.disposition == "fail"]
        return VERDICT_FAIL, True, f"stage zero failed: {failed}"
    if any(j.outcome == FAIL for j in judges):
        failed = [j.judge for j in judges if j.outcome == FAIL]
        return VERDICT_FAIL, True, f"judge FAIL: {failed}"
    if any(j.outcome == OPERATIONAL_ABSTAIN for j in judges):
        abstained = [j.judge for j in judges if j.outcome == OPERATIONAL_ABSTAIN]
        return (VERDICT_INCOMPLETE, False,
                f"operational abstention: {abstained}; attempt incomplete,"
                " suite re-runnable")
    has_attention = any(r.disposition == "attention" for r in invariants)
    has_terminal_abstain = any(j.outcome == TERMINAL_ABSTAIN for j in judges)
    has_advisory = any(f.severity == "advisory" for j in judges for f in j.findings)
    if has_attention or has_terminal_abstain or has_advisory:
        reasons = []
        if has_attention:
            reasons.append("invariant attention")
        if has_terminal_abstain:
            reasons.append("terminal abstention")
        if has_advisory:
            reasons.append("advisory findings")
        return VERDICT_ATTENTION, True, "; ".join(reasons)
    return VERDICT_PASS, True, "stage zero and all judges passed"


def _assert_prompt_provenance(judge: str, template: str) -> None:
    """Judge prompt construction draws only from the persisted snapshot and
    content model: the template's single substitution point is
    {payload_json}, which build_judge_payload fills exclusively from those
    two objects. Any other brace placeholder would be a second data path
    into the prompt and refuses to run."""
    placeholders = set(re.findall(r"\{[a-z_]+\}", template))
    if placeholders != {"{payload_json}"}:
        raise RuntimeError(
            f"judge '{judge}' prompt template must carry exactly the"
            f" {{payload_json}} placeholder; found {sorted(placeholders)}."
            " Judge inputs may draw only from the persisted snapshot and"
            " content model.")


def _model_set_status(judges: tuple[JudgeResult, ...]) -> str:
    """Classification of the run's observed model set over every call:
    'mixed' when any judge's calls disagree, 'unreported' when any call in a
    ran judge carried no identity (or a ran judge made a call that never
    reported), 'no-model-calls' when no judge ran, else 'consistent'. Mixed
    and unreported runs are non-demonstrating for the corpus table."""
    ran = [j for j in judges if j.outcome != NOT_RUN]
    if not ran:
        return "no-model-calls"
    if any(len(set(j.models)) > 1 for j in ran):
        return "mixed"
    if any("unreported" in j.models or not j.models for j in ran):
        return "unreported"
    return "consistent"


class _ReservationHeartbeat:
    """Concurrent fenced heartbeat covering the whole claimed attempt, stage
    zero and model calls included, on its own repository connection (the
    shipped generation heartbeat pattern). A zero-row renewal permanently
    stops the worker."""

    def __init__(self, repo_factory: Callable[[], AbstractContextManager[PackageRepository]],
                 version_id: str, suite_version: str, owner: str):
        self._repo_factory = repo_factory
        self._version_id = version_id
        self._suite_version = suite_version
        self._owner = owner
        self._stop = threading.Event()
        self.lost = threading.Event()
        self.error: Exception | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            with self._repo_factory() as repo:
                while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                    if not repo.renew_gauntlet_reservation(
                            self._version_id, self._suite_version, self._owner,
                            RESERVATION_SECONDS):
                        # A zero-row renewal is a DEFINITE loss of ownership:
                        # the result is discarded and the reservation is left
                        # alone, since it now belongs to a successor.
                        self.lost.set()
                        return
        except Exception as e:
            # An infrastructure failure is not a loss of ownership: the worker
            # still holds the reservation, so it stops AND releases, rather
            # than stranding the claim until expiry.
            self.error = e
            self.lost.set()

    def stop_and_join(self) -> None:
        self._stop.set()
        self._thread.join()


LOST_DETAIL = ("reservation lost before completion; result discarded (a"
               " successor owns this suite's attempt)")


def _discarded() -> GauntletRunResult:
    return GauntletRunResult(None, None, LOST_DETAIL)


class GauntletRunner:
    """Runs one attempt against a VERIFIED (or APPROVED) version: claim,
    policy snapshot, stage zero, judges (sequentially, no fail-fast),
    write-once evidence objects, verdict reduction, fenced run insert."""

    def __init__(self, repo: PackageRepository, storage: StorageAdapter,
                 extractor: PdfTextExtractor,
                 judge_models: Mapping[str, object],
                 prompt_templates: Mapping[str, str],
                 heartbeat_repo_factory: Callable[
                     [], AbstractContextManager[PackageRepository]] | None = None):
        if heartbeat_repo_factory is None:
            # Fail fast and loudly rather than silently sharing the caller's
            # connection: the heartbeat renews from its own THREAD, and a
            # sqlite connection is thread-affine, so a shared one turns every
            # run outliving one heartbeat interval into an infrastructure
            # failure that records nothing. A caller that really has only one
            # connection (an in-memory database) must say so explicitly by
            # passing `lambda: nullcontext(repo)`.
            raise ValueError(
                "GauntletRunner requires a heartbeat_repo_factory: the"
                " reservation heartbeat runs in its own thread and needs its"
                " own repository connection")
        self._repo = repo
        self._storage = storage
        self._extractor = extractor
        self._judge_models = judge_models
        self._prompt_templates = prompt_templates
        self._heartbeat_repo_factory = heartbeat_repo_factory

    def run(self, version: PackageVersion, profile: dict,
            policies: dict) -> GauntletRunResult:
        if version.status not in ("VERIFIED", "APPROVED"):
            return GauntletRunResult(
                None, None,
                f"only a VERIFIED or APPROVED version is judged; '{version.id}'"
                f" is {version.status}")
        owner = f"gauntlet-{int(time.time() * 1000)}-{threading.get_ident()}"
        # Atomic admission: the claim itself rejects a suite that already has
        # a complete attempt; a live concurrent worker makes the loser stop.
        if not self._repo.claim_gauntlet_reservation(
                version.id, SUITE_VERSION, owner, RESERVATION_SECONDS):
            return GauntletRunResult(
                None, None,
                f"a Gauntlet run for suite {SUITE_VERSION} is already running"
                f" on '{version.id}'")
        heartbeat = _ReservationHeartbeat(
            self._heartbeat_repo_factory, version.id, SUITE_VERSION, owner)
        heartbeat.start()
        try:
            try:
                return self._run_claimed(version, profile, policies, owner, heartbeat)
            finally:
                heartbeat.stop_and_join()
        except Exception as e:
            # The recovery boundary over the whole claimed attempt: a storage
            # or infrastructure failure anywhere (the policy-snapshot write or
            # read-back, the evidence writes) must not strand the claim. The
            # reservation is released through the fenced repository operation,
            # so the suite is immediately re-runnable rather than blocked
            # until expiry, and the failure is reported instead of raised.
            return GauntletRunResult(
                None, None, self._release(version, owner, e))

    def _release(self, version: PackageVersion, owner: str,
                 error: Exception) -> str:
        failure = f"{type(error).__name__}: {error}"
        try:
            released = self._repo.release_gauntlet_reservation(
                version.id, SUITE_VERSION, owner)
        except Exception as release_error:
            return (f"the attempt failed ({failure}) and the reservation could"
                    f" not be released ({type(release_error).__name__}:"
                    f" {release_error}); it expires in"
                    f" {RESERVATION_SECONDS}s")
        if not released:
            return (f"the attempt failed ({failure}); the reservation was"
                    " already gone (expired or taken over by a successor)")
        return (f"the attempt failed and recorded nothing ({failure}); the"
                " reservation was released and the suite is immediately"
                " re-runnable")

    def _provider_versions(self, judges: tuple[JudgeResult, ...]) -> dict:
        """The backend version behind each judge that actually ran, asked once
        per run. A backend that cannot be asked records 'unavailable'; nothing
        is inferred, and a failure here never affects the verdict."""
        versions = {}
        for judge in judges:
            if judge.outcome == NOT_RUN:
                continue
            adapter = self._judge_models.get(judge.judge)
            try:
                version = adapter.provider_version()
            except Exception:
                version = "unavailable"
            versions[judge.judge] = (version if isinstance(version, str) and version
                                     else "unavailable")
        return versions

    def _heartbeat_stop(self, version: PackageVersion, owner: str,
                        heartbeat: _ReservationHeartbeat) -> GauntletRunResult:
        """The worker stopped because the heartbeat said so. The two causes
        are not the same: a zero-row renewal means ownership genuinely moved,
        so the result is discarded and the reservation is left to its owner;
        an infrastructure error means the worker still holds the reservation,
        so it is released and the suite is immediately re-runnable."""
        if heartbeat.error is not None:
            return GauntletRunResult(
                None, None,
                "the heartbeat could not renew the reservation; "
                + self._release(version, owner, heartbeat.error))
        return _discarded()

    def _run_claimed(self, version: PackageVersion, profile: dict,
                     policies: dict, owner: str,
                     heartbeat: _ReservationHeartbeat) -> GauntletRunResult:
        run_id = new_id("grun")
        run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Cooperative fencing: the heartbeat's loss is checked before every
        # expensive phase (evidence load, each judge call, the evidence
        # writes), not only at the final consume. A fenced-out worker stops
        # immediately instead of spending model calls on a result that the
        # consume would discard anyway.
        if heartbeat.lost.is_set():
            return self._heartbeat_stop(version, owner, heartbeat)

        # The policy snapshot: write-once canonical bytes of exactly the
        # policy inputs stage zero consumes; the hash covers the stored bytes
        # read back, attesting what storage actually holds. Stage zero then
        # consumes ONLY the decoded read-back value, validated fail closed
        # against the canonical schema.
        policy_locator = gauntlet_object_locator(run_id, "policy_snapshot.json")
        self._storage.write_text_new(
            policy_locator, policy_snapshot_json(build_policy_snapshot(profile, policies)))
        policy_bytes = self._storage.read_bytes(policy_locator)
        policy_hash = hashlib.sha256(policy_bytes).hexdigest()

        # Judges read the persisted snapshot and artifact of this specific
        # version, never live state. Every malformed persisted shape becomes
        # an audit-integrity FAIL recorded through the normal fenced append,
        # never an exception that aborts before it.
        invariants: tuple[InvariantResult, ...] | None = None
        cv = snapshot = None
        snapshot_bytes = artifact_bytes = b""
        try:
            policy_snapshot = parse_policy_snapshot(policy_bytes)
        except PolicySnapshotError as e:
            invariants = (InvariantResult(
                "audit-integrity", "fail",
                f"policy snapshot read-back failed validation: {e}"),)
        if invariants is None:
            if heartbeat.lost.is_set():
                return self._heartbeat_stop(version, owner, heartbeat)
            # The stored objects are read INSIDE the boundary: a missing or
            # unreadable snapshot or artifact object is an audit record that
            # does not verify, exactly like a malformed one, and is refused
            # through the normal fenced append with judges NOT_RUN and zero
            # model calls. An abort here would record nothing and strand the
            # reservation until expiry. The read is caught broadly because a
            # StorageAdapter may fail in any way its backend does.
            try:
                snapshot_bytes = self._storage.read_bytes(
                    version.context_snapshot_locator)
                artifact_bytes = self._storage.read_bytes(version.artifact_locator)
            except Exception as e:
                invariants = (InvariantResult(
                    "audit-integrity", "fail",
                    f"a persisted audit object could not be read:"
                    f" {type(e).__name__}: {e}"),)
        if invariants is None:
            try:
                snapshot = json.loads(snapshot_bytes)
                cv = parse_cv_model(version.content_model_json or "")
            except (json.JSONDecodeError, UnicodeDecodeError, CvModelError,
                    TypeError) as e:
                invariants = (InvariantResult(
                    "audit-integrity", "fail",
                    f"persisted audit bundle is malformed: {e}"),)
            else:
                # Belt and braces: any unexpected exception out of the
                # extractor or the invariant rules still becomes a fenced
                # audit-integrity FAIL append, never an abort before it.
                try:
                    extracted_text = self._extractor.extract_layout(artifact_bytes)
                    invariants = run_invariants(
                        cv=cv, snapshot=snapshot, snapshot_bytes=snapshot_bytes,
                        input_context_hash=version.input_context_hash,
                        artifact_bytes=artifact_bytes,
                        artifact_hash=version.artifact_hash,
                        verifier_report_json=version.verifier_report_json,
                        ats_report_json=version.ats_report_json,
                        extracted_text=extracted_text,
                        policy_snapshot=policy_snapshot)
                except Exception as e:
                    cv = None  # no judge may run over state that blew up
                    invariants = (InvariantResult(
                        "audit-integrity", "fail",
                        f"stage zero raised unexpectedly over the persisted"
                        f" bundle: {type(e).__name__}: {e}"),)

        if invariants_passed(invariants) and cv is not None:
            # Provenance assertion (the security boundary's precondition):
            # judge prompts are the versioned template plus a payload built
            # ONLY from the persisted snapshot and content model objects
            # decoded above; the template may carry no other injection point.
            for judge in JUDGES:
                _assert_prompt_provenance(judge, self._prompt_templates[judge])
            ran: list[JudgeResult] = []
            for judge in JUDGES:
                # Checked before every model call: a worker that lost the
                # fence spends nothing further.
                if heartbeat.lost.is_set():
                    return self._heartbeat_stop(version, owner, heartbeat)
                ran.append(run_judge(
                    judge, self._judge_models[judge],
                    build_judge_prompt(judge, self._prompt_templates[judge],
                                       cv, snapshot),
                    cv, snapshot["renderable_grounding_view"]["profile"]))
            judges = tuple(ran)
        else:
            # Any stage-zero failure ends the run: no judge runs, no model
            # tokens are spent.
            judges = tuple(not_run(judge) for judge in JUDGES)

        verdict, complete, stop_reason = reduce_verdict(invariants, judges)

        # Replayable inputs and auditable results: the ordered per-judge
        # attempt transcript (every prompt actually sent, retry feedback
        # included, with its completion or adapter error), split across the
        # two write-once evidence objects, each bound to the run row by a
        # recorded hash of the stored bytes.
        if heartbeat.lost.is_set():
            return self._heartbeat_stop(version, owner, heartbeat)
        prompts_locator = gauntlet_object_locator(run_id, "prompt_inputs.json")
        self._storage.write_text_new(prompts_locator, json.dumps(
            {j.judge: [entry["prompt"] for entry in j.transcript] for j in judges},
            indent=2, sort_keys=True))
        prompts_hash = hashlib.sha256(
            self._storage.read_bytes(prompts_locator)).hexdigest()
        completions_locator = gauntlet_object_locator(run_id, "raw_completions.json")
        self._storage.write_text_new(completions_locator, json.dumps(
            {j.judge: [{"completion": entry["completion"]} if "completion" in entry
                       else {"error": entry["error"]} for entry in j.transcript]
             for j in judges},
            indent=2, sort_keys=True))
        completions_hash = hashlib.sha256(
            self._storage.read_bytes(completions_locator)).hexdigest()

        # Model identity: observed per call, aggregated over ALL calls of the
        # run; a mixed or unreported set marks the run non-demonstrating for
        # table purposes (the package verdict is unaffected).
        resolved_models = {j.judge: list(j.models) for j in judges}
        report = {
            "suite_version": SUITE_VERSION,
            "resolved_models": resolved_models,
            # Provider identity, observed once per run beside model identity.
            # Where a backend reports no resolved model (the Codex CLI does
            # not), this is what bounds the claim, and a change in it voids a
            # demonstration table exactly as a model-set change does.
            "provider_versions": self._provider_versions(judges),
            "model_set_status": _model_set_status(judges),
            "run_at": run_at,
            "policy_snapshot_locator": policy_locator,
            "policy_snapshot_hash": policy_hash,
            "invariants": [{"rule": r.rule, "disposition": r.disposition,
                            "detail": r.detail} for r in invariants],
            "judges": [j.report_dict() for j in judges],
            "verdict": verdict,
            "stop_reason": stop_reason,
        }
        # The heartbeat is stopped and joined before the consume transaction;
        # the fenced consume then decides atomically whether this worker still
        # owns the attempt.
        heartbeat.stop_and_join()
        try:
            run = self._repo.insert_gauntlet_run(
                version.id, SUITE_VERSION, owner,
                run_id=run_id, complete=1 if complete else 0,
                report_json=json.dumps(report, indent=2, sort_keys=True),
                prompt_inputs_locator=prompts_locator,
                prompt_inputs_hash=prompts_hash,
                raw_completions_locator=completions_locator,
                raw_completions_hash=completions_hash,
                resolved_models_json=json.dumps(resolved_models, sort_keys=True),
                policy_snapshot_locator=policy_locator,
                policy_snapshot_hash=policy_hash)
        except ReservationLostError:
            # A zero-row consume discards the stale worker's result entirely.
            return _discarded()
        return GauntletRunResult(run, verdict, stop_reason)
