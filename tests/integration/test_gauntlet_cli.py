"""Gauntlet CLI wiring end to end over the real pipeline artifacts (spec: the
scope's decisions/gauntlet-design.md, "Unattended execution", "The approval
gate", "CLI"): generate runs the Gauntlet inline after VERIFIED, show lists
runs newest first with the pending status, review reconciles then gates, the
override records, and export/import carries the run evidence. Judges are fake
adapters; no real CLI is ever called."""

import json
import sqlite3

import pytest

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.portability import export_archive, import_archive
from adapters.storage.sqlite_packages import SqlitePackageRepository
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from apps.cli import package_cmd
from domain.gauntlet import SUITE_VERSION
from domain.gauntlet_judges import JUDGES
from domain.packages import APPROVED, VERIFIED
from domain.ports import ModelAdapter

# Reuse the seeded environment of the pipeline suite.
from test_package_pipeline import FakeModel, _model_json, _seed


class FakeJudge(ModelAdapter):
    def __init__(self, responses, model="judge-model"):
        self.responses = list(responses)
        self.model = model
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self.responses.pop(0)

    def complete_with_meta(self, prompt: str):
        return self.complete(prompt), {"model": self.model}


def _judges(response='{"verdict": "PASS", "findings": []}', repeat=6):
    return {j: FakeJudge([response] * repeat) for j in JUDGES}


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "instance" / "open-career.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    storage = LocalStorageAdapter(tmp_path / "instance")
    _seed(conn)
    yield conn, storage
    conn.close()


def test_generate_runs_the_gauntlet_inline_and_records_the_run(env):
    conn, storage = env
    judges = _judges()
    said = []
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, said.append,
        judge_models=judges)
    assert result.status == VERIFIED
    repo = SqlitePackageRepository(conn)
    run = repo.effective_gauntlet_run(result.version_id, SUITE_VERSION)
    assert run is not None and run.verdict == "PASS"
    assert all(j.calls == 1 for j in judges.values())
    assert any("gauntlet run" in line for line in said)


def test_show_lists_runs_newest_first_and_pending_status(env):
    conn, storage = env
    # No judges passed: the version stays gauntlet-pending (the crash-window
    # class), surfaced durably.
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    said = []
    package_cmd.run_show(conn, result.version_id, True, said.append)
    payload = json.loads("".join(said))
    assert payload["gauntlet"] == "pending" and payload["gauntlet_runs"] == []
    # Judged: the run appears; runs list newest first.
    package_cmd.run_gauntlet(conn, storage, _judges(), result.version_id,
                             False, lambda _s: None)
    said = []
    package_cmd.run_show(conn, result.version_id, True, said.append)
    payload = json.loads("".join(said))
    assert payload["gauntlet"] == "adjudicated"
    assert [r["verdict"] for r in payload["gauntlet_runs"]] == ["PASS"]


def test_review_reconciles_then_approves_on_pass(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    said = []
    package_cmd.run_review(conn, storage, None, result.version_id, 1,
                           lambda _p: "a", said.append, judge_models=_judges())
    repo = SqlitePackageRepository(conn)
    assert repo.get_version(result.version_id).status == APPROVED
    assert any("judging now" in line for line in said)
    (decision,) = repo.list_approval_decisions(result.version_id)
    assert decision[3] == "PASS" and decision[4] == 0


FAIL_RESPONSE_TEMPLATE = ('{"verdict": "FAIL", "findings": [{"element_id":'
                          ' "%s", "severity": "blocking", "quote": "%s",'
                          ' "message": "recombines unsupported claims"%s}]}')


def _failing_judges(conn, result_version_id):
    """A Truth Judge FAIL grounded in the real generated content (the fake
    response must survive the evidence grammar)."""
    version = SqlitePackageRepository(conn).get_version(result_version_id)
    content = json.loads(version.content_model_json)
    entry = content["experiences"][0]
    element = f"experiences[{entry['experience_id']}].bullet[0]"
    bullet = entry["bullets"][0]
    quote = bullet["text"][:30]
    fact_ids = json.dumps(bullet["fact_ids"])
    truth_fail = FAIL_RESPONSE_TEMPLATE % (element, quote,
                                           f', "fact_ids": {fact_ids}')
    judges = _judges()
    judges["truth"] = FakeJudge([truth_fail])
    return judges


def test_fail_blocks_accept_and_the_override_records(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    package_cmd.run_gauntlet(conn, storage, _failing_judges(conn, result.version_id),
                             result.version_id, False, lambda _s: None)
    repo = SqlitePackageRepository(conn)
    assert repo.effective_gauntlet_run(result.version_id, SUITE_VERSION).verdict == "FAIL"
    # Plain accept is refused with the override path named.
    with pytest.raises(package_cmd.PackageCliError, match="accept-despite-gauntlet"):
        package_cmd.run_review(conn, storage, None, result.version_id, 1,
                               lambda _p: "a", lambda _s: None)
    # The override approves and records, never silently.
    said = []
    package_cmd.run_review(conn, storage, None, result.version_id, 1,
                           lambda _p: "a", said.append,
                           accept_despite="reviewed by hand; claim is accurate")
    assert repo.get_version(result.version_id).status == APPROVED
    (decision,) = repo.list_approval_decisions(result.version_id)
    assert decision[4] == 1 and "reviewed by hand" in decision[5]
    assert any("DESPITE" in line for line in said)


def test_findings_from_feeds_blocking_findings_into_regeneration(env):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    package_cmd.run_gauntlet(conn, storage, _failing_judges(conn, result.version_id),
                             result.version_id, False, lambda _s: None)
    model = FakeModel([_model_json(conn)])
    prompts = []
    original = model.complete
    model.complete = lambda p: (prompts.append(p), original(p))[1]
    result2 = package_cmd.run_generate(
        conn, storage, model, "FDE", 1, lambda _s: None,
        findings_from=result.version_id)
    assert result2.status == VERIFIED
    assert "failed the Gauntlet" in prompts[0]
    assert "recombines unsupported claims" in prompts[0]


def test_gauntlet_json_output_and_archive_roundtrip_of_run_evidence(env, tmp_path):
    conn, storage = env
    result = package_cmd.run_generate(
        conn, storage, FakeModel([_model_json(conn)]), "FDE", 1, lambda _s: None)
    said = []
    package_cmd.run_gauntlet(conn, storage, _judges(), result.version_id,
                             True, said.append)
    payload = json.loads("".join(said))
    assert payload["report"]["verdict"] == "PASS"
    assert payload["report"]["suite_version"] == SUITE_VERSION
    # Export bundles the run evidence hash-verified; import restores it.
    db = tmp_path / "instance" / "open-career.sqlite3"
    archive = tmp_path / "export.zip"
    conn.commit()
    export_archive(db, tmp_path / "instance", archive)
    target_root = tmp_path / "restored"
    import_archive(target_root / "open-career.sqlite3", target_root, archive)
    run = SqlitePackageRepository(conn).effective_gauntlet_run(
        result.version_id, SUITE_VERSION)
    for locator in (run.policy_snapshot_locator, run.prompt_inputs_locator,
                    run.raw_completions_locator):
        assert (target_root / locator).exists()
