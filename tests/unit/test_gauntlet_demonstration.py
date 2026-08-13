"""Self-test of the corpus demonstration harness mechanics
(scripts/gauntlet_demonstration.py) on a SYNTHETIC corpus with fake judges:
case.md parsing, mechanical broken-element derivation, expectation checks,
and the discrimination protocol's backup/mutate/restore operations. The
frozen corpus and real model calls stay outside the default suite."""

import json
import sys
from pathlib import Path

import pytest
from test_gauntlet_invariants import PROFILE, extracted_text, make_case, make_cv
from test_gauntlet_judges import FakeJudgeModel, _output
from test_gauntlet_run import FixedExtractor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from gauntlet_demonstration import (  # noqa: E402
    apply_mutation,
    broken_elements,
    copy_tree_for_trial,
    parse_case_md,
)
from gauntlet_demonstration import run_case as _run_case  # noqa: E402
from gauntlet_demonstration import run_demonstration as _run_demonstration  # noqa: E402
from gauntlet_demonstration import (  # noqa: E402
    run_discrimination_trial as _run_discrimination_trial,
)

from domain.gauntlet_judges import JUDGES  # noqa: E402


# Bundles are derived evidence and never live inside the corpus tree: every
# synthetic corpus in this module keeps them in a sibling directory, and these
# thin wrappers pass that external root the way the operator passes --bundles.

def bundles_for(corpus: Path) -> Path:
    return corpus.parent / "bundles"


def run_case(case_dir, corpus_dir, judges, workdir, **kwargs):
    kwargs.setdefault("bundles_root", bundles_for(corpus_dir))
    return _run_case(case_dir, corpus_dir, judges, workdir, **kwargs)


def run_demonstration(corpus_dir, judges, *args, **kwargs):
    kwargs.setdefault("bundles_root", bundles_for(corpus_dir))
    return _run_demonstration(corpus_dir, judges, *args, **kwargs)


def run_discrimination_trial(case_dir, corpus_dir, **kwargs):
    kwargs.setdefault("bundles_root", bundles_for(corpus_dir))
    return _run_discrimination_trial(case_dir, corpus_dir, **kwargs)


def _write_case(corpus: Path, name: str, cv, case_md: str, bundles_root: Path,
                snapshot=None) -> Path:
    """One synthetic case: the frozen inputs under the corpus, the derived
    bundle under an EXTERNAL bundles root (never inside the corpus tree)."""
    import hashlib

    import gauntlet_demonstration as gd
    from domain.gauntlet_invariants import build_policy_snapshot, policy_snapshot_json

    case_dir = corpus / name
    case_dir.mkdir(parents=True)
    bundle = bundles_root / name
    bundle.mkdir(parents=True)
    (case_dir / "case.md").write_text(case_md)
    fixtures = make_case(cv=cv, snapshot=snapshot)
    # Per-case artifact bytes, so a byte-keyed extractor can serve each
    # case its own extracted text across a multi-case demonstration.
    artifact = f"%PDF-1.4 {name}".encode()
    (case_dir / "content_model.json").write_text(cv.to_json())
    # The frozen case inputs the freeze manifest covers and the bundle derives
    # from.
    (case_dir / "context_snapshot.json").write_bytes(fixtures["snapshot_bytes"])
    context = gd.derive_context_json((case_dir / "context_snapshot.json").read_bytes())
    policy = policy_snapshot_json(build_policy_snapshot(PROFILE, {}))
    extracted = extracted_text(cv)
    objects = {
        "context.json": context.encode(),
        "content_model.json": cv.to_json().encode(),
        "cv.pdf": artifact,
        "extracted.txt": extracted.encode(),
        "verifier_report.json": fixtures["verifier_report_json"].encode(),
        "ats_report.json": fixtures["ats_report_json"].encode(),
        "policy_snapshot.json": policy.encode(),
    }
    for filename, data in objects.items():
        (bundle / filename).write_bytes(data)
    hashes = {key: hashlib.sha256(objects[filename]).hexdigest()
              for filename, key in gd.HASHED_BUNDLE_OBJECTS.items()}
    hashes["frozen_inputs"] = _frozen_inputs(case_dir)
    (bundle / "hashes.json").write_text(json.dumps(hashes))
    return case_dir


def _frozen_inputs(case_dir: Path) -> dict:
    """The same rule the freeze and the bundle binding use: every *.json and
    *.md case input outside the derived bundle."""
    import hashlib

    return {p.relative_to(case_dir).as_posix():
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(case_dir.rglob("*"))
            if p.is_file() and "bundle" not in p.relative_to(case_dir).parts
            and p.suffix in (".json", ".md")}


def freeze_corpus(corpus: Path) -> Path:
    """Write the sha256 freeze manifest over every case's frozen inputs, in
    `sha256sum` format with paths relative to the corpus root (the shape of
    the committed tests/gauntlet/corpus/CORPUS_MANIFEST.sha256)."""
    import gauntlet_demonstration as gd

    lines = []
    for case_dir in sorted(d for d in corpus.iterdir() if d.is_dir()):
        for name, digest in sorted(_frozen_inputs(case_dir).items()):
            lines.append(f"{digest}  ./{case_dir.name}/{name}")
    manifest = corpus / gd.MANIFEST_NAME
    manifest.write_text("\n".join(lines) + "\n")
    return manifest


def corpus_extractor(corpus: Path):
    """An extractor keyed by artifact bytes: each case extracts to its own
    content model's section text."""
    from domain.cv_model import parse_cv_model
    from domain.ports import PdfTextExtractor

    texts = {}
    for case_dir in corpus.iterdir():
        bundle = bundles_for(corpus) / case_dir.name
        if bundle.is_dir():
            cv = parse_cv_model((bundle / "content_model.json").read_text())
            texts[(bundle / "cv.pdf").read_bytes()] = extracted_text(cv)

    class MapExtractor(PdfTextExtractor):
        def extract_layout(self, pdf_bytes: bytes) -> str:
            return texts[pdf_bytes]

    return MapExtractor()


CLEAN_MD = ("# case\n- **Class**: clean control\n"
            "- **Expected catching layer**: none; the run must PASS\n")
BROKEN_MD = ("# case\n- **Class**: fabricated-claim\n"
             "- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`):"
             " at least one valid blocking finding\n")
STAGE_ZERO_MD = ("# case\n- **Class**: wrong-work-authorization\n"
                 "- **Expected catching layer**: stage zero, deterministic\n")


@pytest.fixture
def corpus(tmp_path):
    corpus = tmp_path / "corpus"
    bundles = bundles_for(corpus)
    _write_case(corpus, "clean-base", make_cv(), CLEAN_MD, bundles)
    _write_case(corpus, "broken-x",
                make_cv(bullet_text="Reduced onboarding time by 40%"), BROKEN_MD,
                bundles)
    freeze_corpus(corpus)
    return corpus


def test_case_md_parsing_and_broken_element_derivation(corpus):
    assert parse_case_md(corpus / "clean-base") == {
        "class": "clean control", "clean": True, "layer": None}
    parsed = parse_case_md(corpus / "broken-x")
    assert parsed["clean"] is False and parsed["layer"] == "truth"
    assert parse_case_md_stage_zero()
    assert broken_elements(corpus / "broken-x", corpus) == {
        "experiences[exp_1].bullet[0]"}


def parse_case_md_stage_zero():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "case.md").write_text(STAGE_ZERO_MD)
        return parse_case_md(Path(d))["layer"] == "stage-zero"


def _judges(truth_response=None):
    judges = {j: FakeJudgeModel([_output()] * 3) for j in JUDGES}
    if truth_response:
        judges["truth"] = FakeJudgeModel([truth_response] * 3)
    return judges


def test_clean_case_runs_and_meets_its_expectation(corpus, tmp_path):
    cv = make_cv()
    record = run_case(corpus / "clean-base", corpus, _judges(),
                      tmp_path / "work", extractor=FixedExtractor(extracted_text(cv)))
    assert record["status"] == "ran" and record["verdict"] == "PASS"
    assert record["ok"] is True
    assert record["model_set_status"] == "consistent"


def test_broken_case_requires_the_declared_catcher_on_the_broken_element(corpus, tmp_path):
    cv = make_cv(bullet_text="Reduced onboarding time by 40%")
    catch = json.dumps({"verdict": "FAIL", "findings": [{
        "element_id": "experiences[exp_1].bullet[0]", "severity": "blocking",
        "quote": "Reduced onboarding time by 40%", "message": "m",
        "fact_ids": ["fact_1"]}]})
    record = run_case(corpus / "broken-x", corpus, _judges(catch),
                      tmp_path / "w1", extractor=FixedExtractor(extracted_text(cv)))
    assert record["ok"] is True and record["verdict"] == "FAIL"
    # The same case escaping (all judges PASS) does not demonstrate.
    record = run_case(corpus / "broken-x", corpus, _judges(),
                      tmp_path / "w2", extractor=FixedExtractor(extracted_text(cv)))
    assert record["ok"] is False


def test_missing_bundle_is_reported_not_skipped(corpus, tmp_path):
    import shutil

    case_dir = _write_case(corpus, "broken-empty", make_cv(), BROKEN_MD,
                           bundles_for(corpus))
    # Frozen inputs, no completed bundle.
    shutil.rmtree(bundles_for(corpus) / "broken-empty")
    freeze_corpus(corpus)
    record = run_case(case_dir, corpus, _judges(), tmp_path / "w")
    assert record["status"] == "no-bundle" and record["ok"] is False


TRUTH_CATCH = json.dumps({"verdict": "FAIL", "findings": [{
    "element_id": "experiences[exp_1].bullet[0]", "severity": "blocking",
    "quote": "Reduced onboarding time by 40%", "message": "m",
    "fact_ids": ["fact_1"]}]})


def _test_registry(tmp_path):
    target = tmp_path / "checker.py"
    target.write_text("RULE_ENABLED = True\n")
    return target, {"fabricated-claim": {
        "target": str(target), "name": "disable-test-rule",
        "old": "RULE_ENABLED = True", "new": "RULE_ENABLED = False"}}


def _leg_record(verdict="FAIL", status="ran", accused=("experiences[exp_1].bullet[0]",),
                failed_invariants=(), ok=None):
    hit = bool(accused)
    return {"case": "broken-x", "status": status, "verdict": verdict,
            "layer": "truth", "clean": False,
            "failed_invariants": list(failed_invariants),
            "blocking_findings": {"truth": list(accused)},
            "ok": hit and verdict == "FAIL" if ok is None else ok}


def test_crashed_mutated_leg_is_an_invalid_trial_never_an_escape(corpus, tmp_path):
    target, registry = _test_registry(tmp_path)
    legs = iter([RuntimeError("leg subprocess crashed"),
                 _leg_record()])  # restored leg catches

    def spawner(case_dir, corpus_dir, workdir, root):
        leg = next(legs)
        if isinstance(leg, Exception):
            raise leg
        return leg

    trial = run_discrimination_trial(corpus / "broken-x", corpus,
                                     registry=registry, leg_spawner=spawner)
    assert trial["valid_trial"] is False
    assert "crashed" in trial["invalid_reason"]
    assert trial["escaped_under_mutation"] is False
    assert trial["discriminates"] is False
    # The out-of-tree fixture was put back.
    assert target.read_text() == "RULE_ENABLED = True\n"


def test_incomplete_leg_is_an_invalid_trial(corpus, tmp_path):
    _target, registry = _test_registry(tmp_path)
    legs = iter([_leg_record(verdict="INCOMPLETE", accused=(), ok=False),
                 _leg_record()])
    trial = run_discrimination_trial(
        corpus / "broken-x", corpus, registry=registry,
        leg_spawner=lambda *_a: next(legs))
    assert trial["valid_trial"] is False
    assert "incomplete" in trial["invalid_reason"]
    assert trial["discriminates"] is False


def test_unrelated_stage_zero_failure_in_a_leg_is_invalid(corpus, tmp_path):
    _target, registry = _test_registry(tmp_path)
    legs = iter([_leg_record(accused=(), failed_invariants=["audit-integrity"],
                             ok=False),
                 _leg_record()])
    trial = run_discrimination_trial(
        corpus / "broken-x", corpus, registry=registry,
        leg_spawner=lambda *_a: next(legs))
    assert trial["valid_trial"] is False
    assert "stage-zero failure" in trial["invalid_reason"]
    assert trial["escaped_under_mutation"] is False


def test_valid_trial_escape_and_catch_with_mutation_applied_per_leg(corpus, tmp_path):
    """A well-formed trial: during the mutated leg the on-disk target carries
    the named mutation; the restored leg sees the original bytes; escape then
    catch discriminates."""
    target, registry = _test_registry(tmp_path)
    seen = []

    def spawner(case_dir, corpus_dir, workdir, root):
        seen.append(target.read_text())
        if len(seen) == 1:  # mutated leg: catcher silent
            return _leg_record(verdict="PASS", accused=(), ok=False)
        return _leg_record()  # restored leg: catcher fires

    trial = run_discrimination_trial(corpus / "broken-x", corpus,
                                     registry=registry, leg_spawner=spawner)
    assert seen == ["RULE_ENABLED = False\n", "RULE_ENABLED = True\n"]
    assert trial["valid_trial"] is True
    assert trial["escaped_under_mutation"] is True
    assert trial["caught_after_restore"] is True
    assert trial["discriminates"] is True
    assert trial["mutation_name"] == "disable-test-rule"


def test_registry_rejects_unknown_class_and_stale_mutations(corpus, tmp_path):
    target, registry = _test_registry(tmp_path)
    # A class with no predeclared entry: arbitrary targets are rejected.
    with pytest.raises(ValueError, match="no predeclared discrimination"):
        run_discrimination_trial(corpus / "clean-base", corpus,
                                 registry=registry,
                                 leg_spawner=lambda *_a: _leg_record())
    # A registry whose named mutation no longer applies to the target.
    target.write_text("something else\n")
    with pytest.raises(ValueError, match="no longer applies"):
        run_discrimination_trial(corpus / "broken-x", corpus,
                                 registry=registry,
                                 leg_spawner=lambda *_a: _leg_record())


def test_shipped_registry_mutations_apply_to_the_real_checkers():
    """The checked registry: every predeclared target exists and its named
    mutation text is present in the on-disk checker source."""
    import gauntlet_demonstration as gd

    for declared_class, entry in gd.MUTATION_REGISTRY.items():
        target = gd._REPO / entry["target"]
        assert target.is_file(), declared_class
        assert entry["old"] in target.read_text(), (
            f"registry mutation '{entry['name']}' for class"
            f" '{declared_class}' no longer applies")


def test_default_leg_spawner_uses_a_fresh_interpreter(monkeypatch, tmp_path, corpus):
    """Each leg spawns sys.executable on the script's _leg subcommand, so
    imports reflect the on-disk (mutated or restored) source; a nonzero exit
    comes back as an error record, never an escape."""
    import gauntlet_demonstration as gd

    calls = []

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(gd.subprocess, "run", fake_run)
    record = gd._default_leg_spawner(corpus / "broken-x", corpus, tmp_path / "leg")
    (argv,) = calls
    import sys as _sys
    assert argv[0] == _sys.executable and "_leg" in argv
    assert record["status"] == "error" and "boom" in record["detail"]


def test_silent_model_move_between_cases_voids_the_record(corpus):
    """The expected resolved-model set from the recorded table: a model
    change between cases makes the record non-demonstrating and says why."""
    judges = {j: FakeJudgeModel([_output()] * 6, model="model-fixed")
              for j in JUDGES}
    # truth silently moves to another model on the second case (cases sort
    # broken-x first, so the catch responses serve its three legs).
    judges["truth"] = FakeJudgeModel(
        [TRUTH_CATCH] * 3 + [_output()] * 3,
        model=["model-a"] * 3 + ["model-b"] * 3)
    record = run_demonstration(
        corpus, judges, expected_models={
            "truth": "model-a", "consistency": "model-fixed",
            "writing": "model-fixed"},
        extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert any("do not match the expected set" in r
               for r in record["non_demonstrating_reasons"])
    assert False in {leg.get("model_set_matches_expected")
                     for case in record["cases"] for leg in case["legs"]}


def test_omitted_or_malformed_expected_models_voids_the_record(corpus):
    judges = {j: FakeJudgeModel([TRUTH_CATCH] + [_output()] * 11,
                                model="model-fixed") for j in JUDGES}
    record = run_demonstration(corpus, judges, expected_models=None,
                               extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert any("mandatory" in r for r in record["non_demonstrating_reasons"])
    # Wrong judge keys are rejected too.
    record = run_demonstration(corpus, judges,
                               expected_models={"truth": "m"},
                               extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert any("do not match the configured judge set" in r
               for r in record["non_demonstrating_reasons"])


def test_matching_expected_models_keeps_the_record_demonstrating(corpus):
    judges = {j: FakeJudgeModel([_output()] * 6, model="model-fixed")
              for j in JUDGES}
    judges["truth"] = FakeJudgeModel([TRUTH_CATCH] * 3 + [_output()] * 3,
                                     model="model-fixed")
    record = run_demonstration(
        corpus, judges, expected_models={j: "model-fixed" for j in JUDGES},
        extractor=corpus_extractor(corpus))
    assert record["non_demonstrating_reasons"] == []
    assert record["demonstrating"] is True
    # Three consecutive passing runs per case, all persisted.
    assert record["runs_per_case"] == 3
    for case in record["cases"]:
        assert len(case["legs"]) == 3 and case["ok"] is True
        assert [o["ok"] for o in case["leg_outcomes"]] == [True, True, True]


def test_a_case_passing_twice_and_failing_once_is_not_demonstrated(corpus):
    """Three consecutive passes are the claim: two of three is a fail."""
    judges = {j: FakeJudgeModel([_output()] * 6, model="model-fixed")
              for j in JUDGES}
    # broken-x runs first: caught, caught, then silent on the third leg.
    judges["truth"] = FakeJudgeModel(
        [TRUTH_CATCH, TRUTH_CATCH, _output()] + [_output()] * 3,
        model="model-fixed")
    record = run_demonstration(
        corpus, judges, expected_models={j: "model-fixed" for j in JUDGES},
        extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    (broken,) = [c for c in record["cases"] if c["case"] == "broken-x"]
    assert [o["ok"] for o in broken["leg_outcomes"]] == [True, True, False]
    assert broken["ok"] is False
    assert any("run 3 of 3): expectation not met" in r
               for r in record["non_demonstrating_reasons"])


def test_a_stage_zero_case_failing_on_an_unrelated_rule_is_not_certified(
        corpus, tmp_path):
    """The declared catching layer names a RULE: certifying
    wrong-work-authorization on a date-coherence or audit-integrity FAIL would
    claim evidence the suite never produced."""
    import dataclasses

    cv = make_cv()
    # Dates that do not cohere, and no authorization assertion anywhere.
    entry = dataclasses.replace(cv.experiences[0], start_date="2024-05",
                                end_date="2022-03")
    cv = dataclasses.replace(cv, experiences=(entry,))
    case_dir = _write_case(corpus, "broken-unrelated", cv,
                           _case_md("wrong-work-authorization",
                                    "stage zero, rule `work-authorization`"),
                           bundles_for(corpus))
    freeze_corpus(corpus)
    record = run_case(case_dir, corpus, _judges(), tmp_path / "w",
                      extractor=FixedExtractor(extracted_text(cv)))
    assert record["verdict"] == "FAIL"
    assert record["failed_invariants"] and \
        "work-authorization" not in record["failed_invariants"]
    assert record["ok"] is False  # a FAIL from another rule is not a catch


def test_the_record_persists_finding_text_not_only_element_ids(corpus, tmp_path):
    """Element ids alone made a real defect (a judge blocking clean summaries)
    undiagnosable from demonstration.json: the quote and message are the
    evidence."""
    cv = make_cv(bullet_text="Reduced onboarding time by 40%")
    catch = json.dumps({"verdict": "FAIL", "findings": [{
        "element_id": "experiences[exp_1].bullet[0]", "severity": "blocking",
        "quote": "Reduced onboarding time by 40%",
        "message": "the fact states no 40 percent figure",
        "fact_ids": ["fact_1"]}]})
    record = run_case(corpus / "broken-x", corpus, _judges(catch),
                      tmp_path / "w", extractor=FixedExtractor(extracted_text(cv)))
    (finding,) = record["judge_findings"]["truth"]
    assert finding["quote"] == "Reduced onboarding time by 40%"
    assert finding["message"] == "the fact states no 40 percent figure"
    assert finding["fact_ids"] == ["fact_1"]
    assert record["judge_findings"]["writing"] == []


def test_a_consistency_finding_counts_when_it_cites_either_named_element(
        corpus, tmp_path):
    """A contradiction is between TWO elements and the case declares both;
    which one the judge puts first is not a property the corpus fixes. The
    real run scored 2 of 3 on cross-section-contradiction purely because one
    leg named the other half of the declared pair first."""
    cv = make_cv(bullet_text="Reduced onboarding time by 40%")
    # The judge names the entry FIRST and the broken bullet second.
    reversed_pair = json.dumps({"verdict": "FAIL", "findings": [{
        "element_id": "experiences[exp_1]", "severity": "blocking",
        "quote": "Acme", "message": "contradicts the bullet beneath it",
        "second_element_id": "experiences[exp_1].bullet[0]",
        "second_quote": "Reduced onboarding time by 40%"}]})
    judges = _judges()
    judges["consistency"] = FakeJudgeModel([reversed_pair] * 3)
    case_md = ("# case\n- **Class**: cross-section-contradiction\n"
               "- **Expected catching layer**: Consistency Judge: a finding"
               " naming two elements\n")
    case_dir = _write_case(corpus, "broken-pair", cv, case_md,
                           bundles_for(corpus))
    freeze_corpus(corpus)
    record = run_case(case_dir, corpus, judges, tmp_path / "w",
                      extractor=FixedExtractor(extracted_text(cv)))
    assert record["verdict"] == "FAIL"
    # Only the second cited element is the diff-derived broken one.
    assert record["blocking_findings"]["consistency"] == ["experiences[exp_1]"]
    assert broken_elements(case_dir, corpus) == {"experiences[exp_1].bullet[0]"}
    assert record["ok"] is True


# -- identity: an unreported model pinned by its provider version ------------
#
# The Codex CLI reports no resolved model (its `codex exec --json` stream
# carries no model field), so 'unreported' is that backend's honest and
# permanent state. The table pins the PAIR (unreported model, provider
# version) and says so in a limitation line.

CODEX_VERSION = "codex-cli 0.145.0"


class SilentModelJudge(FakeJudgeModel):
    """Reports no model identity, and a provider version instead."""

    def __init__(self, responses, provider_version=CODEX_VERSION):
        super().__init__(responses, model=None)
        self._provider_version = provider_version

    def provider_version(self) -> str:
        return self._provider_version


def _unreported_truth_judges(provider_version=CODEX_VERSION):
    judges = {j: FakeJudgeModel([_output()] * 6, model="model-fixed")
              for j in JUDGES}
    judges["truth"] = SilentModelJudge([TRUTH_CATCH] * 3 + [_output()] * 3,
                                       provider_version=provider_version)
    return judges


def _expected(**overrides):
    expected = {j: "model-fixed" for j in JUDGES}
    expected.update(overrides)
    return expected


def test_expected_unreported_with_a_matching_provider_version_demonstrates(corpus):
    record = run_demonstration(
        corpus, _unreported_truth_judges(),
        expected_models=_expected(truth="unreported"),
        expected_provider_versions={"truth": CODEX_VERSION},
        extractor=corpus_extractor(corpus))
    assert record["non_demonstrating_reasons"] == []
    assert record["demonstrating"] is True
    assert {"truth": CODEX_VERSION} in [
        {k: v for k, v in observed.items() if k == "truth"}
        for observed in record["observed_provider_versions"]]


def test_a_provider_version_mismatch_voids_the_table(corpus):
    record = run_demonstration(
        corpus, _unreported_truth_judges(provider_version="codex-cli 0.146.0"),
        expected_models=_expected(truth="unreported"),
        expected_provider_versions={"truth": CODEX_VERSION},
        extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert any("does not match the expected" in r and "provider version" in r
               for r in record["non_demonstrating_reasons"])


def test_an_unreported_identity_where_a_real_model_was_expected_voids_the_table(corpus):
    record = run_demonstration(
        corpus, _unreported_truth_judges(),
        expected_models=_expected(truth="a-real-model"),
        extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert any("do not match the expected set" in r
               for r in record["non_demonstrating_reasons"])


def test_expecting_unreported_without_pinning_the_provider_version_is_refused(corpus):
    record = run_demonstration(
        corpus, _unreported_truth_judges(),
        expected_models=_expected(truth="unreported"),
        extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert any("pin their provider version" in r
               for r in record["non_demonstrating_reasons"])


def test_any_unreported_identity_carries_a_verbatim_limitation_line(corpus):
    record = run_demonstration(
        corpus, _unreported_truth_judges(),
        expected_models=_expected(truth="unreported"),
        expected_provider_versions={"truth": CODEX_VERSION},
        extractor=corpus_extractor(corpus))
    (line,) = record["limitations"]
    assert "Model identity limitation (truth judge)" in line
    assert "no resolved model identity" in line
    assert "codex exec --json" in line and "no model field" in line
    # Accurate for BOTH CLIs: neither exposes a resolved model, so the text
    # never claims the Codex CLI sits behind a Claude-backed judge.
    assert "modelUsage but no model or modelName key" in line
    assert CODEX_VERSION in line
    # It travels per case too, for the published table.
    assert all(case["limitations"] == [line] for case in record["cases"]
               if case["layer"] != "stage-zero")
    # A fully reported set carries no limitation line.
    judges = {j: FakeJudgeModel([_output()] * 6, model="model-fixed")
              for j in JUDGES}
    judges["truth"] = FakeJudgeModel([TRUTH_CATCH] * 3 + [_output()] * 3,
                                     model="model-fixed")
    clean = run_demonstration(corpus, judges, expected_models=_expected(),
                              extractor=corpus_extractor(corpus))
    assert clean["limitations"] == [] and clean["demonstrating"] is True


def test_the_limitation_line_names_the_right_backend_per_judge(corpus):
    """Every judge here reports no identity (both CLIs behave this way), each
    with its own provider version: no line may assert another judge's
    backend or another judge's version."""
    judges = {j: SilentModelJudge([_output()] * 6,
                                  provider_version=f"cli-{j} 1.0")
              for j in JUDGES}
    judges["truth"] = SilentModelJudge([TRUTH_CATCH] * 3 + [_output()] * 3,
                                       provider_version=CODEX_VERSION)
    record = run_demonstration(
        corpus, judges,
        expected_models={j: "unreported" for j in JUDGES},
        expected_provider_versions={"truth": CODEX_VERSION,
                                    "consistency": "cli-consistency 1.0",
                                    "writing": "cli-writing 1.0"},
        extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is True
    by_judge = {j: [line for line in record["limitations"]
                    if f"({j} judge)" in line] for j in JUDGES}
    assert all(len(lines) == 1 for lines in by_judge.values())
    assert CODEX_VERSION in by_judge["truth"][0]
    for judge in ("consistency", "writing"):
        line = by_judge[judge][0]
        assert f"cli-{judge} 1.0" in line
        assert CODEX_VERSION not in line
        assert "For the Truth Judge" not in line


# -- frozen-corpus integrity --------------------------------------------------

def test_a_tampered_case_input_refuses_to_run(corpus, tmp_path):
    """The corpus claim rests on blind-authored content: a case input that no
    longer matches the freeze never runs."""
    (corpus / "broken-x" / "case.md").write_text(BROKEN_MD + "\nedited later\n")
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity" and record["ok"] is False
    assert "does not match the freeze" in record["detail"]
    assert all(model.calls == 0 for model in _judges().values())


def test_a_case_missing_from_the_manifest_refuses_to_run(corpus, tmp_path):
    import gauntlet_demonstration as gd

    manifest = corpus / gd.MANIFEST_NAME
    manifest.write_text("".join(
        line + "\n" for line in manifest.read_text().splitlines()
        if "/broken-x/" not in line))
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "no entries in" in record["detail"]


def test_an_input_added_after_the_freeze_refuses_to_run(corpus, tmp_path):
    (corpus / "broken-x" / "extra_notes.md").write_text("smuggled in\n")
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "not in the freeze" in record["detail"]


def test_a_stale_bundle_is_refused_never_silently_used(corpus, tmp_path):
    """A bundle must name the exact frozen inputs it was built from."""
    bundle = bundles_for(corpus) / "broken-x"
    hashes = json.loads((bundle / "hashes.json").read_text())
    hashes["frozen_inputs"]["content_model.json"] = "0" * 64
    (bundle / "hashes.json").write_text(json.dumps(hashes))
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "stale or substituted" in record["detail"]


def test_a_bundle_without_the_binding_is_refused(corpus, tmp_path):
    bundle = bundles_for(corpus) / "broken-x"
    hashes = json.loads((bundle / "hashes.json").read_text())
    del hashes["frozen_inputs"]
    (bundle / "hashes.json").write_text(json.dumps(hashes))
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "records no frozen_inputs" in record["detail"]


# The bundle's own frozen_inputs claim proves nothing about what the bundle
# CONTAINS: each of these edits keeps the claimed provenance correct and would
# seed a different, self-consistent package if the derived objects were not
# verified against the frozen files themselves.

def test_an_edited_bundle_content_model_is_refused_despite_a_correct_claim(
        corpus, tmp_path):
    bundle = bundles_for(corpus) / "broken-x"
    edited = make_cv(bullet_text="Something else entirely")
    (bundle / "content_model.json").write_text(edited.to_json())
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "is not the frozen" in record["detail"]
    # The claimed binding is still intact, which is exactly the point.
    hashes = json.loads((bundle / "hashes.json").read_text())
    assert hashes["frozen_inputs"] == _frozen_inputs(corpus / "broken-x")


def test_an_edited_bundle_snapshot_is_refused_despite_a_correct_claim(
        corpus, tmp_path):
    bundle = bundles_for(corpus) / "broken-x"
    context = json.loads((bundle / "context.json").read_text())
    context["renderable_grounding_view"]["profile"]["location"] = "Berlin, Germany"
    text = json.dumps(context, indent=2, sort_keys=True)
    (bundle / "context.json").write_text(text)
    # Even with the recorded hash updated to the edited bytes, the derivation
    # from the frozen input no longer holds.
    hashes = json.loads((bundle / "hashes.json").read_text())
    import hashlib
    hashes["input_context_hash"] = hashlib.sha256(text.encode()).hexdigest()
    (bundle / "hashes.json").write_text(json.dumps(hashes))
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "mechanical derivation" in record["detail"]


def test_a_swapped_artifact_is_refused(corpus, tmp_path):
    bundle = bundles_for(corpus) / "broken-x"
    (bundle / "cv.pdf").write_bytes(b"%PDF-1.4 a different document")
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w")
    assert record["status"] == "corpus-integrity"
    assert "cv.pdf does not match its recorded artifact_hash" in record["detail"]


def test_a_bundle_resolving_inside_the_corpus_is_refused(corpus, tmp_path):
    """An in-corpus bundle sits in the frozen lane but outside manifest
    coverage, so it could be introduced or altered without invalidating
    anything and then consumed as evidence."""
    record = run_case(corpus / "broken-x", corpus, _judges(), tmp_path / "w",
                      bundles_root=corpus)
    assert record["status"] == "corpus-integrity"
    assert "inside the frozen corpus tree" in record["detail"]


def test_a_missing_manifest_voids_the_whole_demonstration(corpus):
    import gauntlet_demonstration as gd

    (corpus / gd.MANIFEST_NAME).unlink()
    record = run_demonstration(corpus, _judges(), expected_models={
        j: "model-fixed" for j in JUDGES}, extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False and record["cases"] == []
    assert any("manifest" in r for r in record["non_demonstrating_reasons"])


def test_an_integrity_failure_marks_the_demonstration_non_demonstrating(corpus):
    (corpus / "broken-x" / "content_model.json").write_text("{}")
    record = run_demonstration(corpus, _judges(), expected_models={
        j: "model-fixed" for j in JUDGES}, extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False
    assert record["cases"] == []  # fail closed: no leg runs
    assert any("does not match the freeze" in r
               for r in record["non_demonstrating_reasons"])


# The declared case set is the corpus, not whatever happens to be on disk.

def test_a_removed_declared_case_voids_the_demonstration(corpus):
    """Deleting a broken case would otherwise let the survivors run clean and
    certify the complete corpus."""
    import shutil

    shutil.rmtree(corpus / "broken-x")
    record = run_demonstration(corpus, _judges(), expected_models={
        j: "model-fixed" for j in JUDGES}, extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False and record["cases"] == []
    assert any("'broken-x' is declared" in r and "absent" in r
               for r in record["non_demonstrating_reasons"])


def test_an_undeclared_case_directory_voids_the_demonstration(corpus):
    (corpus / "broken-smuggled").mkdir()
    (corpus / "broken-smuggled" / "case.md").write_text(BROKEN_MD)
    record = run_demonstration(corpus, _judges(), expected_models={
        j: "model-fixed" for j in JUDGES}, extractor=corpus_extractor(corpus))
    assert record["demonstrating"] is False and record["cases"] == []
    assert any("'broken-smuggled' is present but undeclared" in r
               for r in record["non_demonstrating_reasons"])


def test_a_single_case_run_never_certifies_the_corpus(corpus):
    judges = {j: FakeJudgeModel([_output()] * 6, model="model-fixed")
              for j in JUDGES}
    judges["truth"] = FakeJudgeModel([TRUTH_CATCH] * 3, model="model-fixed")
    record = run_demonstration(
        corpus, judges, "broken-x",
        expected_models={j: "model-fixed" for j in JUDGES},
        extractor=corpus_extractor(corpus))
    # The legs still ran (a debugging aid), but the record cannot certify.
    assert len(record["cases"]) == 1 and record["cases"][0]["ok"] is True
    assert record["demonstrating"] is False
    assert any("partial run never certifies" in r
               for r in record["non_demonstrating_reasons"])


def test_the_shipped_corpus_verifies_against_its_committed_freeze():
    """The real frozen corpus, against the committed manifest."""
    import gauntlet_demonstration as gd

    manifest = gd.load_manifest(gd.DEFAULT_CORPUS)
    assert len(manifest) >= 40
    for case_dir in sorted(d for d in gd.DEFAULT_CORPUS.iterdir() if d.is_dir()):
        gd.verify_case_integrity(case_dir, gd.DEFAULT_CORPUS, manifest)


# -- the bundle builder never writes into the frozen corpus -------------------

def test_the_bundle_builder_refuses_to_write_into_the_frozen_corpus(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import build_corpus_bundle as bcb

    for candidate in (bcb.FROZEN_CORPUS,
                      bcb.FROZEN_CORPUS / "clean-base" / "bundle",
                      bcb.FROZEN_CORPUS / ".." / "corpus" / "x"):
        with pytest.raises(ValueError, match="frozen corpus"):
            bcb.check_out_dir(candidate)
    # Anywhere else is fine.
    assert bcb.check_out_dir(tmp_path / "bundles" / "case") == \
        (tmp_path / "bundles" / "case").resolve()


def test_the_bundle_builder_records_the_frozen_input_hashes(tmp_path):
    import hashlib

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import build_corpus_bundle as bcb

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "context_snapshot.json").write_text("{}")
    (case_dir / "content_model.json").write_text("{}")
    (case_dir / "case.md").write_text("# case\n")
    hashes = bcb.frozen_input_hashes(case_dir, (tmp_path / "out").resolve())
    assert set(hashes) == {"context_snapshot.json", "content_model.json",
                           "case.md"}
    assert hashes["case.md"] == hashlib.sha256(b"# case\n").hexdigest()
    # A case missing a required input is an error, never a partial binding.
    (case_dir / "content_model.json").unlink()
    with pytest.raises(ValueError, match="missing frozen inputs"):
        bcb.frozen_input_hashes(case_dir, (tmp_path / "out").resolve())


# -- the shipped registry, exercised end to end -------------------------------
#
# Every shipped mutation is run through the real fresh-subprocess trial legs
# against a synthetic corpus with a FAKED model layer (no real model calls, no
# network): the mutation must actually remove the declared catcher's valid
# blocking evidence, and the byte-identical restore must bring it back.

BROKEN_BULLET = "Reduced onboarding time by 40%"
SPONSORSHIP_CLAIM = "Requires visa sponsorship for enterprise deployments"

_CATCH_FINDING = {
    "truth": {"element_id": "experiences[exp_1].bullet[0]", "severity": "blocking",
              "quote": BROKEN_BULLET, "message": "m", "fact_ids": ["fact_1"]},
    "consistency": {"element_id": "experiences[exp_1].bullet[0]",
                    "severity": "blocking", "quote": BROKEN_BULLET, "message": "m",
                    "second_element_id": "experiences[exp_1]",
                    "second_quote": "Acme"},
    "writing": {"element_id": "experiences[exp_1].bullet[0]",
                "severity": "blocking", "quote": BROKEN_BULLET, "message": "m"},
}


def _case_md(declared_class: str, layer_line: str) -> str:
    return (f"# case\n- **Class**: {declared_class}\n"
            f"- **Expected catching layer**: {layer_line}\n")


def _registry_corpus(root: Path):
    """clean-base plus one synthetic case per shipped registry class, with the
    per-case fake judge responses each trial leg must serve."""
    import gauntlet_demonstration as gd

    from domain.gauntlet_invariants import build_work_authorization_projection
    from test_gauntlet_invariants import make_snapshot

    corpus = root / "corpus"
    bundles = bundles_for(corpus)
    _write_case(corpus, "clean-base", make_cv(), CLEAN_MD, bundles)
    responses = {}
    for declared_class in gd.MUTATION_REGISTRY:
        name = f"broken-{declared_class}"
        if declared_class == "wrong-work-authorization":
            # Stage zero: an authorization-class assertion in the rendered
            # text that matches no allowed form of the policy snapshot. Its
            # fact statement carries the same words, so grounding still
            # re-verifies and only the work-authorization rule fails.
            snapshot = make_snapshot()
            snapshot["renderable_grounding_view"]["facts"]["fact_1"]["statement"] = \
                SPONSORSHIP_CLAIM
            _write_case(corpus, name, make_cv(bullet_text=SPONSORSHIP_CLAIM),
                        _case_md(declared_class,
                                 "stage zero, rule `work-authorization`"),
                        bundles, snapshot=snapshot)
            responses[name] = {j: _output() for j in JUDGES}
            continue
        judge = gd._CLASS_JUDGE[declared_class]
        _write_case(corpus, name, make_cv(bullet_text=BROKEN_BULLET),
                    _case_md(declared_class,
                             f"{judge.capitalize()} Judge (`gauntlet_{judge}.md`):"
                             " at least one valid blocking finding"), bundles)
        responses[name] = {j: _output() for j in JUDGES}
        responses[name][judge] = _output("FAIL", [_CATCH_FINDING[judge]])
    # The projection the policy snapshot carries is the one stage zero judges.
    assert build_work_authorization_projection(PROFILE)["allowed_forms"]
    freeze_corpus(corpus)
    return corpus, responses


_LEG_HELPER = '''
import json, shutil, sys, tempfile
from pathlib import Path

REPO = Path(sys.argv[1])
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests" / "unit"))
import gauntlet_demonstration as gd  # also puts packages/ on the path

from domain.cv_model import parse_cv_model
from domain.ports import ModelAdapter, PdfTextExtractor
from test_gauntlet_invariants import extracted_text

case_dir, corpus, out, responses_path, bundles = (Path(p) for p in sys.argv[2:7])
responses = json.loads(responses_path.read_text())


class FakeAdapter(ModelAdapter):
    """A canned response; NO real model call and no network."""

    def __init__(self, text):
        self._text = text

    def complete(self, prompt):
        return self._text

    def complete_with_meta(self, prompt):
        return self._text, {"model": "fake-model-1"}


texts = {}
for case in corpus.iterdir():
    bundle = bundles / case.name
    if bundle.is_dir():
        cv = parse_cv_model((bundle / "content_model.json").read_text())
        texts[(bundle / "cv.pdf").read_bytes()] = extracted_text(cv)


class MapExtractor(PdfTextExtractor):
    def extract_layout(self, pdf_bytes):
        return texts[pdf_bytes]


judges = {j: FakeAdapter(t) for j, t in responses[case_dir.name].items()}
workdir = Path(tempfile.mkdtemp(prefix="gauntlet-fake-leg-"))
try:
    record = gd.run_case(case_dir, corpus, judges, workdir,
                         extractor=MapExtractor(), bundles_root=bundles)
finally:
    shutil.rmtree(workdir, ignore_errors=True)
out.write_text(json.dumps(record, indent=2, sort_keys=True))
'''


def _fake_leg_spawner(helper: Path, responses_path: Path):
    """The real trial shape: one FRESH subprocess per leg, so the on-disk
    (mutated or restored) checker source is what executes, with the model
    layer faked."""
    import subprocess

    def spawn(case_dir, corpus_dir, workdir, root=None):
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "leg.json"
        repo = root or Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable, str(helper), str(repo), str(case_dir),
             str(corpus_dir), str(out), str(responses_path),
             str(bundles_for(corpus_dir))],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
        return json.loads(out.read_text())

    return spawn


@pytest.fixture(scope="module")
def registry_corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("registry-trials")
    corpus, responses = _registry_corpus(root)
    helper = root / "fake_leg.py"
    helper.write_text(_LEG_HELPER)
    responses_path = root / "responses.json"
    responses_path.write_text(json.dumps(responses))
    return corpus, _fake_leg_spawner(helper, responses_path)


def _registry_classes():
    import gauntlet_demonstration as gd

    return sorted(gd.MUTATION_REGISTRY)


@pytest.mark.parametrize("declared_class", _registry_classes())
def test_every_shipped_registry_mutation_removes_the_catchers_evidence(
        declared_class, registry_corpus):
    corpus, spawner = registry_corpus
    trial = run_discrimination_trial(corpus / f"broken-{declared_class}", corpus,
                                     leg_spawner=spawner)
    assert trial["valid_trial"] is True, trial["invalid_reason"]
    assert trial["escaped_under_mutation"] is True, trial["mutated_run"]
    assert trial["caught_after_restore"] is True, trial["restored_run"]
    assert trial["discriminates"] is True


def test_a_verdict_only_judge_mutation_does_not_discriminate(registry_corpus):
    """The regression behind the judge-layer registry entries: a mutation that
    disables only the blocking-finding-to-FAIL mapping leaves the finding in
    the report, so the catcher still hits and the leg is not an escape. Such a
    mutation can never demonstrate anything."""
    corpus, spawner = registry_corpus
    verdict_only = {"fabricated-claim": {
        "target": "packages/domain/gauntlet_judges.py",
        "name": "disable-blocking-finding-fail",
        "old": '        if any(f.severity == "blocking" for f in valid):\n'
               "            outcome = FAIL",
        "new": '        if False and any(f.severity == "blocking" for f in valid):\n'
               "            outcome = FAIL"}}
    trial = run_discrimination_trial(corpus / "broken-fabricated-claim", corpus,
                                     registry=verdict_only, leg_spawner=spawner)
    assert trial["escaped_under_mutation"] is False
    assert trial["discriminates"] is False


def test_a_trial_never_mutates_the_live_checkout(corpus, tmp_path):
    """The residue defect: trials used to edit real source files in the live
    checkout and restore them afterwards, which corrupted concurrent sessions
    and made trials fail spuriously. Each leg now runs against its own
    throwaway copy, so the checkout is untouched at every moment, not merely
    afterwards."""
    import gauntlet_demonstration as gd

    live = gd._REPO / "packages" / "domain" / "gauntlet_judges.py"
    before = live.read_bytes()
    seen_roots = []

    def spawner(case_dir, corpus_dir, workdir, root):
        seen_roots.append(root)
        # DURING the leg, the live file is pristine and the copy carries the
        # mutation.
        assert live.read_bytes() == before
        copied = root / "packages" / "domain" / "gauntlet_judges.py"
        return {"case": case_dir.name, "status": "ran", "verdict": "FAIL",
                "layer": "truth", "clean": False, "failed_invariants": [],
                "blocking_findings": {"truth": ["experiences[exp_1].bullet[0]"]},
                "mutated_copy": "findings, valid, invalid = [], (), ()"
                                 in copied.read_text(),
                "ok": True}

    trial = run_discrimination_trial(corpus / "broken-x", corpus,
                                     leg_spawner=spawner)
    assert live.read_bytes() == before
    assert trial["mutated_run"]["mutated_copy"] is True
    assert trial["restored_run"]["mutated_copy"] is False
    assert "never" in trial["ran_against"]
    # Two distinct throwaway trees, both gone afterwards.
    assert len(set(map(str, seen_roots))) == 2
    assert not any(root.exists() for root in seen_roots)


def test_a_tree_copy_carries_the_code_and_not_the_baggage(tmp_path):
    import gauntlet_demonstration as gd

    root = copy_tree_for_trial(tmp_path / "tree")
    assert (root / "packages" / "domain" / "gauntlet_judges.py").is_file()
    assert (root / "scripts" / "gauntlet_demonstration.py").is_file()
    assert (root / "migrations").is_dir()
    for skipped in (".git", ".venv", "instance"):
        assert not (root / skipped).exists()
    assert gd._REPO != root


def test_a_mutation_that_does_not_apply_is_an_error_never_a_no_op(tmp_path):
    target = tmp_path / "checker.py"
    target.write_text("RULES = ['date-coherence']\n")
    diff = apply_mutation(target, "['date-coherence']", "[]")
    assert "date-coherence" in diff and target.read_text() == "RULES = []\n"
    with pytest.raises(ValueError, match="not found"):
        apply_mutation(target, "no such text", "x")
