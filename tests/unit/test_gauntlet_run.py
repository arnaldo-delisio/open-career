"""Run orchestration tests (spec: the scope's decisions/gauntlet-design.md,
"Verdict, report, and lifecycle"): verdict reduction in the design's
precedence order, and the runner end to end over sqlite with fake adapters
(no real CLI, no real renderer)."""

import hashlib
import json
import sqlite3

import pytest
from test_gauntlet_invariants import (
    PROFILE,
    extracted_text,
    make_case,
    make_cv,
    make_snapshot,
)
from test_gauntlet_judges import FakeJudgeModel, _finding, _output

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_packages import SqlitePackageRepository
from domain.gauntlet import (
    SUITE_VERSION,
    VERDICT_ATTENTION,
    VERDICT_FAIL,
    VERDICT_INCOMPLETE,
    VERDICT_PASS,
    GauntletRunner,
    reduce_verdict,
)
from domain.gauntlet_invariants import InvariantResult
from domain.gauntlet_judges import (
    FAIL,
    JUDGES,
    NOT_RUN,
    OPERATIONAL_ABSTAIN,
    PASS,
    TERMINAL_ABSTAIN,
    JudgeFinding,
    JudgeResult,
)
from domain.ports import PdfTextExtractor


def _inv(rule="r", disposition="pass"):
    return InvariantResult(rule, disposition, "d")


def _judge(judge="truth", outcome=PASS, findings=()):
    return JudgeResult(judge=judge, outcome=outcome, findings=tuple(findings),
                       invalid_findings=(), attempts=1, models=("m",))


def _advisory():
    return JudgeFinding(element_id="summary", severity="advisory", quote="q",
                        message="m")


# -- reduction precedence -----------------------------------------------------

def test_stage_zero_failure_is_a_complete_fail():
    verdict, complete, _ = reduce_verdict((_inv(disposition="fail"),), ())
    assert (verdict, complete) == (VERDICT_FAIL, True)


def test_valid_fail_is_terminal_even_beside_operational_abstain():
    verdict, complete, reason = reduce_verdict(
        (_inv(),),
        (_judge("truth", FAIL), _judge("consistency", OPERATIONAL_ABSTAIN),
         _judge("writing", PASS)))
    assert (verdict, complete) == (VERDICT_FAIL, True)
    assert "truth" in reason


def test_operational_abstain_alone_leaves_the_attempt_incomplete():
    verdict, complete, _ = reduce_verdict(
        (_inv(),),
        (_judge("truth", OPERATIONAL_ABSTAIN), _judge("consistency", PASS),
         _judge("writing", PASS)))
    assert (verdict, complete) == (VERDICT_INCOMPLETE, False)


def test_regrounding_unsupported_with_clean_judges_reduces_to_attention():
    verdict, complete, reason = reduce_verdict(
        (_inv("regrounding", "attention"),),
        tuple(_judge(j, PASS) for j in JUDGES))
    assert (verdict, complete) == (VERDICT_ATTENTION, True)
    assert "invariant attention" in reason


def test_terminal_abstain_and_advisory_findings_cap_at_attention():
    verdict, complete, _ = reduce_verdict(
        (_inv(),), (_judge("truth", TERMINAL_ABSTAIN), _judge("consistency", PASS),
                    _judge("writing", PASS)))
    assert (verdict, complete) == (VERDICT_ATTENTION, True)
    verdict, complete, _ = reduce_verdict(
        (_inv(),), (_judge("truth", PASS, (_advisory(),)),
                    _judge("consistency", PASS), _judge("writing", PASS)))
    assert (verdict, complete) == (VERDICT_ATTENTION, True)


def test_all_clean_is_pass():
    verdict, complete, _ = reduce_verdict(
        (_inv(),), tuple(_judge(j, PASS) for j in JUDGES))
    assert (verdict, complete) == (VERDICT_PASS, True)


# -- the runner over sqlite ---------------------------------------------------

class FixedExtractor(PdfTextExtractor):
    def __init__(self, text):
        self._text = text

    def extract_layout(self, pdf_bytes: bytes) -> str:
        return self._text


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "instance" / "open-career.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.execute(
            "INSERT INTO role_families (id, name, rationale) VALUES ('rf_1', 'FDE', 'r')")
    storage = LocalStorageAdapter(tmp_path / "instance")
    yield conn, storage
    conn.close()


def _verified_version(repo, storage, case):
    package = repo.get_or_create_base_package("rf_1")
    v = repo.reserve_version(package.id, "owner-a", 60)
    storage.write_bytes_new("ctx.json", case["snapshot_bytes"])
    storage.write_bytes_new("cv.pdf", case["artifact_bytes"])
    repo.finalize_verified(
        v.id, "owner-a", 1,
        content_model_json=case["cv"].to_json(),
        context_snapshot_locator="ctx.json",
        input_context_hash=case["input_context_hash"],
        verifier_report_json=case["verifier_report_json"],
        ats_report_json=case["ats_report_json"],
        artifact_locator="cv.pdf", artifact_hash=case["artifact_hash"])
    return repo.get_version(v.id)


def _runner(repo, storage, case, judge_models, heartbeat_factory=None):
    return GauntletRunner(repo, storage, FixedExtractor(case["extracted_text"]),
                          judge_models,
                          {j: "{payload_json}" for j in JUDGES},
                          heartbeat_repo_factory=(heartbeat_factory
                                                 or _same_connection_factory(repo)))


def _same_connection_factory(repo):
    """These tests drive the runner on one connection deliberately: the
    heartbeat interval is never reached, so the thread makes no call. The
    runner refuses to ASSUME a shared connection, so the intent is stated."""
    from contextlib import nullcontext

    return lambda: nullcontext(repo)


def heartbeat_factory_for(db_path):
    """A dedicated connection per heartbeat thread, the way the CLI and the
    demonstration harness build it."""
    from contextlib import contextmanager

    @contextmanager
    def factory():
        conn = sqlite3.connect(db_path)
        try:
            yield SqlitePackageRepository(conn)
        finally:
            conn.close()

    return factory


def _clean_judges():
    return {j: FakeJudgeModel([_output()], model=f"model-{j}") for j in JUDGES}


def test_clean_run_records_pass_with_evidence_objects(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_PASS and result.run.complete == 1
    report = json.loads(result.run.report_json)
    assert report["suite_version"] == SUITE_VERSION
    assert report["resolved_models"] == {j: [f"model-{j}"] for j in JUDGES}
    assert report["model_set_status"] == "consistent"
    # Provider identity, observed once per run beside model identity (the
    # fakes report the ModelAdapter default).
    assert report["provider_versions"] == {j: "unavailable" for j in JUDGES}
    # Write-once evidence objects exist and their recorded hashes bind them.
    for locator, digest in (
            (result.run.policy_snapshot_locator, result.run.policy_snapshot_hash),
            (result.run.prompt_inputs_locator, result.run.prompt_inputs_hash),
            (result.run.raw_completions_locator, result.run.raw_completions_hash)):
        data = storage.read_bytes(locator)
        assert hashlib.sha256(data).hexdigest() == digest
    prompts = json.loads(storage.read_text(result.run.prompt_inputs_locator))
    assert set(prompts) == set(JUDGES)
    # The reservation was consumed; the effective run resolves.
    assert repo.effective_gauntlet_run(version.id, SUITE_VERSION).id == result.run.id


def test_provider_versions_are_observed_once_per_run(env):
    """Provider identity is what bounds a claim when a backend reports no
    model: it is observed, never inferred, and asked once per run."""
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)

    class VersionedModel(FakeJudgeModel):
        def __init__(self, responses):
            super().__init__(responses, model=None)  # reports no identity
            self.version_calls = 0

        def provider_version(self) -> str:
            self.version_calls += 1
            return "codex-cli 0.145.0"

    judges = _clean_judges()
    judges["truth"] = VersionedModel([_output()])
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    report = json.loads(result.run.report_json)
    assert report["provider_versions"]["truth"] == "codex-cli 0.145.0"
    assert report["resolved_models"]["truth"] == ["unreported"]
    assert judges["truth"].version_calls == 1  # once per run, not per call


def test_a_provider_version_failure_never_affects_the_verdict(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)

    class BrokenVersion(FakeJudgeModel):
        def provider_version(self) -> str:
            raise OSError("cannot exec the CLI")

    judges = _clean_judges()
    judges["writing"] = BrokenVersion([_output()], model="model-writing")
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_PASS
    report = json.loads(result.run.report_json)
    assert report["provider_versions"]["writing"] == "unavailable"


def test_stage_zero_failure_spends_no_model_tokens(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    # A recorded hash the stored bytes no longer match: the audit bundle does
    # not verify, so the package is judged by nobody.
    conn.execute("UPDATE package_versions SET artifact_hash = ? WHERE id = ?",
                 ("0" * 64, version.id))
    conn.commit()
    version = repo.get_version(version.id)
    judges = _clean_judges()
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_FAIL and result.run.complete == 1
    assert all(model.calls == 0 for model in judges.values())
    report = json.loads(result.run.report_json)
    assert all(j["verdict"] == NOT_RUN for j in report["judges"])


def test_judge_fail_beside_outage_completes_with_fail(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = {
        "truth": FakeJudgeModel([_output("FAIL", [_finding("truth")])]),
        "consistency": FakeJudgeModel(["garbage"] * 3),  # operational abstain
        "writing": FakeJudgeModel([_output()]),
    }
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_FAIL and result.run.complete == 1
    report = json.loads(result.run.report_json)
    by_judge = {j["judge"]: j["verdict"] for j in report["judges"]}
    assert by_judge == {"truth": FAIL, "consistency": OPERATIONAL_ABSTAIN,
                        "writing": PASS}


def test_outage_alone_is_an_incomplete_rerunnable_attempt(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    judges["truth"] = FakeJudgeModel(["garbage"] * 3)
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_INCOMPLETE and result.run.complete == 0
    # The reservation was released through the same fenced consume: the suite
    # is immediately re-runnable.
    retry = _runner(repo, storage, case, _clean_judges()).run(
        repo.get_version(version.id), PROFILE, {})
    assert retry.verdict == VERDICT_PASS and retry.run.attempt == 2


def test_second_adjudication_of_a_complete_suite_is_refused(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    _runner(repo, storage, case, _clean_judges()).run(version, PROFILE, {})
    from domain.packages import PackageStateError
    with pytest.raises(PackageStateError, match="complete Gauntlet attempt"):
        _runner(repo, storage, case, _clean_judges()).run(
            repo.get_version(version.id), PROFILE, {})


def test_mixed_model_set_is_classified_on_the_run_record(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    judges["truth"] = FakeJudgeModel(["garbage", _output()],
                                     model=["model-a", "model-b"])
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    report = json.loads(result.run.report_json)
    assert report["resolved_models"]["truth"] == ["model-a", "model-b"]
    assert report["model_set_status"] == "mixed"
    # Non-demonstrating classification never touches the package verdict.
    assert result.verdict == VERDICT_PASS


def test_unreported_model_set_is_classified(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    judges["writing"] = FakeJudgeModel([_output()], model=None)  # reports nothing
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert json.loads(result.run.report_json)["model_set_status"] == "unreported"


def test_transcript_evidence_carries_every_attempt_prompt_and_completion(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    judges["consistency"] = FakeJudgeModel(["garbage", _output()])
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    prompts = json.loads(storage.read_text(result.run.prompt_inputs_locator))
    completions = json.loads(storage.read_text(result.run.raw_completions_locator))
    assert len(prompts["consistency"]) == 2
    assert "failed schema validation" in prompts["consistency"][1]
    assert completions["consistency"][0] == {"completion": "garbage"}
    assert completions["consistency"][1] == {"completion": _output()}
    assert len(prompts["truth"]) == 1


class _CorruptingStorage(LocalStorageAdapter):
    """Returns corrupted bytes when the policy snapshot is read back."""

    def read_bytes(self, relative_path: str) -> bytes:
        data = super().read_bytes(relative_path)
        if relative_path.endswith("policy_snapshot.json"):
            return b'{"unexpected": true}'
        return data


def test_policy_snapshot_readback_failing_validation_is_an_audit_fail(env, tmp_path):
    conn, _ = env
    storage = _CorruptingStorage(tmp_path / "instance")
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    # Recorded through the normal fenced append, judges never ran.
    assert result.verdict == VERDICT_FAIL and result.run.complete == 1
    report = json.loads(result.run.report_json)
    (invariant,) = report["invariants"]
    assert invariant["rule"] == "audit-integrity"
    assert "policy snapshot read-back" in invariant["detail"]
    assert all(j["verdict"] == NOT_RUN for j in report["judges"])
    assert all(model.calls == 0 for model in judges.values())


def test_malformed_persisted_content_model_is_an_audit_fail_not_an_exception(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    conn.execute("UPDATE package_versions SET content_model_json = '[1, 2]'"
                 " WHERE id = ?", (version.id,))
    conn.commit()
    version = repo.get_version(version.id)
    result = _runner(repo, storage, case, _clean_judges()).run(version, PROFILE, {})
    assert result.verdict == VERDICT_FAIL and result.run.complete == 1
    (invariant,) = json.loads(result.run.report_json)["invariants"]
    assert invariant["rule"] == "audit-integrity"
    assert "malformed" in invariant["detail"]


@pytest.mark.parametrize("locator_column", ["context_snapshot_locator",
                                            "artifact_locator"])
def test_a_missing_persisted_object_is_a_fenced_audit_fail(env, locator_column):
    """A missing stored object is an audit record that does not verify: it is
    refused through the normal fenced append with judges NOT_RUN and zero
    model calls, never an abort that records nothing and strands the
    reservation until expiry."""
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    conn.execute(f"UPDATE package_versions SET {locator_column} = 'gone.bin'"
                 " WHERE id = ?", (version.id,))
    conn.commit()
    version = repo.get_version(version.id)
    judges = _clean_judges()
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_FAIL and result.run.complete == 1
    (invariant,) = json.loads(result.run.report_json)["invariants"]
    assert invariant["rule"] == "audit-integrity"
    assert "could not be read" in invariant["detail"]
    assert all(j["verdict"] == NOT_RUN
               for j in json.loads(result.run.report_json)["judges"])
    assert all(model.calls == 0 for model in judges.values())
    # The reservation was consumed through the same fenced append, not
    # stranded: the effective run resolves to this record.
    assert repo.effective_gauntlet_run(version.id, SUITE_VERSION).id == result.run.id


def test_unexpected_stage_zero_exception_becomes_a_fenced_audit_fail(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)

    class ExplodingExtractor(PdfTextExtractor):
        def extract_layout(self, pdf_bytes: bytes) -> str:
            raise RuntimeError("pdftotext blew up")

    judges = _clean_judges()
    runner = GauntletRunner(repo, storage, ExplodingExtractor(), judges,
                            {j: "{payload_json}" for j in JUDGES},
                            heartbeat_repo_factory=_same_connection_factory(repo))
    result = runner.run(version, PROFILE, {})
    # Recorded through the normal fenced append, never an abort before it.
    assert result.verdict == VERDICT_FAIL and result.run.complete == 1
    (invariant,) = json.loads(result.run.report_json)["invariants"]
    assert invariant["rule"] == "audit-integrity"
    assert "raised unexpectedly" in invariant["detail"]
    assert all(model.calls == 0 for model in judges.values())


def test_a_lost_fence_stops_before_the_next_judge_call(env, monkeypatch):
    """Cooperative fencing: the heartbeat's loss is checked before every
    expensive phase, so a fenced-out worker stops immediately instead of
    spending the remaining model calls on a result the consume discards."""
    import threading

    import domain.gauntlet as gauntlet_module

    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    heartbeats = []

    class FakeHeartbeat:
        def __init__(self, *_args, **_kwargs):
            self.lost = threading.Event()
            self.error = None
            heartbeats.append(self)

        def start(self):
            pass

        def stop_and_join(self):
            pass

    monkeypatch.setattr(gauntlet_module, "_ReservationHeartbeat", FakeHeartbeat)
    judges = _clean_judges()

    class LosingModel(FakeJudgeModel):
        def complete(self, prompt: str) -> str:
            heartbeats[0].lost.set()  # renewal failed during this call
            return super().complete(prompt)

    judges["truth"] = LosingModel([_output()], model="model-truth")
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.run is None and "discarded" in result.detail
    # The first judge ran; nothing after it spent a call, and no run was
    # recorded for the abandoned attempt.
    assert judges["truth"].calls == 1
    assert judges["consistency"].calls == 0 and judges["writing"].calls == 0
    assert repo.effective_gauntlet_run(version.id, SUITE_VERSION) is None
    # Ownership genuinely moved: the reservation belongs to its successor now
    # and this worker must NOT release it.
    assert conn.execute("SELECT COUNT(*) FROM gauntlet_reservations"
                        " WHERE package_version_id = ?",
                        (version.id,)).fetchone()[0] == 1


def test_a_heartbeat_infrastructure_error_releases_the_still_owned_reservation(
        env, monkeypatch):
    """An infrastructure failure in the heartbeat is not a loss of ownership:
    the worker still holds the reservation, so it stops AND releases instead
    of stranding the claim until expiry."""
    import threading

    import domain.gauntlet as gauntlet_module

    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    heartbeats = []

    class FailingHeartbeat:
        def __init__(self, *_args, **_kwargs):
            self.lost = threading.Event()
            self.error = None
            heartbeats.append(self)

        def start(self):
            pass

        def stop_and_join(self):
            pass

    monkeypatch.setattr(gauntlet_module, "_ReservationHeartbeat", FailingHeartbeat)
    judges = _clean_judges()

    class FailingModel(FakeJudgeModel):
        def complete(self, prompt: str) -> str:
            heartbeats[0].error = OSError("heartbeat connection died")
            heartbeats[0].lost.set()
            return super().complete(prompt)

    judges["truth"] = FailingModel([_output()], model="model-truth")
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.run is None
    assert "heartbeat connection died" in result.detail
    assert "immediately re-runnable" in result.detail
    assert judges["consistency"].calls == 0
    assert conn.execute("SELECT COUNT(*) FROM gauntlet_reservations"
                        " WHERE package_version_id = ?",
                        (version.id,)).fetchone()[0] == 0
    # Immediately re-runnable, with no waiting out the reservation.
    retry = _runner(repo, storage, case, _clean_judges()).run(
        repo.get_version(version.id), PROFILE, {})
    assert retry.verdict == VERDICT_PASS


class _FailingWriteStorage(LocalStorageAdapter):
    """Raises on the first write whose locator ends with the named suffix."""

    def __init__(self, root, failing_suffix):
        super().__init__(root)
        self._failing_suffix = failing_suffix

    def write_text_new(self, relative_path: str, text: str) -> None:
        if relative_path.endswith(self._failing_suffix):
            raise OSError(f"storage backend is down for {relative_path}")
        super().write_text_new(relative_path, text)


@pytest.mark.parametrize("failing_suffix,spent_tokens", [
    ("policy_snapshot.json", False),   # before any judge call
    ("prompt_inputs.json", True),      # after every judge call
])
def test_a_storage_failure_releases_the_reservation_and_stays_rerunnable(
        env, tmp_path, failing_suffix, spent_tokens):
    """A storage failure anywhere in the claimed attempt must not strand the
    claim: nothing is recorded, the reservation is released through the fenced
    repository operation, and the suite is immediately re-runnable."""
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    broken = _FailingWriteStorage(tmp_path / "instance", failing_suffix)
    judges = _clean_judges()
    result = _runner(repo, broken, case, judges).run(version, PROFILE, {})
    assert result.run is None and result.verdict is None
    assert "storage backend is down" in result.detail
    assert "immediately re-runnable" in result.detail
    assert any(model.calls for model in judges.values()) is spent_tokens
    # Nothing recorded, and no reservation left behind.
    assert repo.list_gauntlet_runs(version.id) == []
    assert conn.execute("SELECT COUNT(*) FROM gauntlet_reservations"
                        " WHERE package_version_id = ?",
                        (version.id,)).fetchone()[0] == 0
    # The immediate rerun succeeds: no waiting out the reservation.
    retry = _runner(repo, storage, case, _clean_judges()).run(
        repo.get_version(version.id), PROFILE, {})
    assert retry.verdict == VERDICT_PASS and retry.run.attempt == 1


def test_a_release_of_a_reservation_a_successor_owns_is_refused(env):
    """The release is fenced like the consume: a stale worker can never drop
    a successor's reservation."""
    conn, _ = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    _, storage = env
    version = _verified_version(repo, storage, case)
    assert repo.claim_gauntlet_reservation(version.id, SUITE_VERSION, "owner-a", 60)
    assert repo.release_gauntlet_reservation(
        version.id, SUITE_VERSION, "owner-b") is False
    assert repo.release_gauntlet_reservation(
        version.id, SUITE_VERSION, "owner-a") is True
    assert repo.release_gauntlet_reservation(
        version.id, SUITE_VERSION, "owner-a") is False


def test_a_run_outliving_the_heartbeat_interval_records_normally(env, tmp_path,
                                                                 monkeypatch):
    """The real demonstration shape: a judge call slower than the heartbeat
    interval, against a real sqlite repository. The heartbeat renews from its
    own thread on its OWN connection, so the run completes and records; a
    shared connection would raise sqlite3.ProgrammingError there and turn
    every slow run into an infrastructure failure that records nothing."""
    import time as _time

    import domain.gauntlet as gauntlet_module

    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    version = _verified_version(repo, storage, case)
    monkeypatch.setattr(gauntlet_module, "HEARTBEAT_INTERVAL_SECONDS", 0.02)

    class SlowModel(FakeJudgeModel):
        def complete(self, prompt: str) -> str:
            _time.sleep(0.15)  # several heartbeat intervals
            return super().complete(prompt)

    judges = _clean_judges()
    judges["truth"] = SlowModel([_output()], model="model-truth")
    runner = _runner(repo, storage, case, judges,
                     heartbeat_factory=heartbeat_factory_for(
                         tmp_path / "instance" / "open-career.sqlite3"))
    result = runner.run(version, PROFILE, {})
    assert result.verdict == VERDICT_PASS and result.run.complete == 1
    assert repo.effective_gauntlet_run(version.id, SUITE_VERSION).id == result.run.id


def test_a_runner_without_a_heartbeat_factory_is_refused(env):
    """Fail fast rather than silently sharing the caller's connection with
    the heartbeat thread."""
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    case = make_case()
    with pytest.raises(ValueError, match="heartbeat_repo_factory"):
        GauntletRunner(repo, storage, FixedExtractor(case["extracted_text"]),
                       _clean_judges(), {j: "{payload_json}" for j in JUDGES})


def test_a_recognized_prior_verifier_spec_caps_at_attention(env):
    """A stored snapshot from a prior spec is a well-formed audit record: it
    is not rerun under new semantics, the judges still run, and the verdict
    caps at ATTENTION (never an audit-integrity FAIL, never a PASS)."""
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    snapshot = make_snapshot()
    snapshot["normalization_spec_version"] = "2"
    case = make_case(snapshot=snapshot)
    trail = json.loads(case["verifier_report_json"])
    trail["final"]["spec_version"] = "2"
    case["verifier_report_json"] = json.dumps(trail)
    version = _verified_version(repo, storage, case)
    judges = _clean_judges()
    result = _runner(repo, storage, case, judges).run(version, PROFILE, {})
    assert result.verdict == VERDICT_ATTENTION and result.run.complete == 1
    report = json.loads(result.run.report_json)
    by_rule = {r["rule"]: r for r in report["invariants"]}
    assert by_rule["audit-integrity"]["disposition"] == "pass"
    assert by_rule["regrounding"]["disposition"] == "attention"
    assert "regrounding-unsupported" in by_rule["regrounding"]["detail"]
    assert all(j["verdict"] == PASS for j in report["judges"])
    assert all(model.calls == 1 for model in judges.values())


def test_non_verified_version_is_never_judged(env):
    conn, storage = env
    repo = SqlitePackageRepository(conn)
    package = repo.get_or_create_base_package("rf_1")
    generating = repo.reserve_version(package.id, "o", 60)
    case = make_case()
    result = _runner(repo, storage, case, _clean_judges()).run(
        generating, PROFILE, {})
    assert result.run is None and "GENERATING" in result.detail
