"""Deliberate Gauntlet corpus demonstration runner (spec: the scope's
decisions/gauntlet-design.md, "The regression corpus"). Never part of the
default suite: judge cases cost real model calls and their determinism is
statistical. The independent test operator runs this after corpus freeze; it
only READS the frozen corpus content and consumes bundles completed by
scripts/build_corpus_bundle.py.

What it does per case, three independent consecutive times (judge determinism
is statistical, so one pass demonstrates nothing; every leg is persisted and
all three must meet the expectation):
- seeds a throwaway instance with the bundle as a VERIFIED package version;
- runs the real GauntletRunner (real judge adapters by default; injectable);
- checks the case's expectation, derived from its case.md-declared class:
  clean cases must PASS; `wrong-work-authorization` must fail at stage zero;
  every other broken case must yield at least one VALID blocking finding from
  its declared catching layer citing the broken element (the accused element
  set is derived mechanically by diffing the case's content model against
  clean-base, the paired-counterfactual property);
- records a machine-readable demonstration record: per-case verdicts,
  catcher validity, accused elements, observed resolved-model sets and the
  run's model-set classification (a mixed or unreported set voids the table).

It also ships the discrimination protocol as automatable operations:
cp-backup with a recorded hash, a named rule-disabling mutation, run (case
must escape), byte-identical restore verified by hash, rerun (case must be
caught). The three-of-three demonstration runs and the published table remain
the test operator's job; this file is the harness.

Usage:
  uv run python scripts/gauntlet_demonstration.py run \
      --expected-models '{"truth": "unreported", "consistency": "...", "writing": "..."}' \
      --expected-provider-versions '{"truth": "codex-cli 0.145.0"}' \
      [--corpus tests/gauntlet/corpus] [--bundles instance/corpus-bundles] \
      [--out demonstration.json] [--case NAME]
  uv run python scripts/gauntlet_demonstration.py discriminate \
      --case NAME [--corpus ...] [--out trial.json]
      (the checker target and named mutation come from the checked
       MUTATION_REGISTRY, keyed by the case's declared class; each trial leg
       runs in a fresh subprocess so the mutated source is what executes)
"""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "packages"))

from adapters.render.pdftext import PopplerPdfTextExtractor      # noqa: E402
from adapters.storage.local import LocalStorageAdapter           # noqa: E402
from adapters.storage.migrations import migrate                  # noqa: E402
from adapters.storage.sqlite_packages import SqlitePackageRepository  # noqa: E402
from domain.cv_model import parse_cv_model                       # noqa: E402
from domain.gauntlet import SUITE_VERSION, GauntletRunner        # noqa: E402
from domain.gauntlet_judges import JUDGES, PROMPT_FILES, element_texts  # noqa: E402
from domain.grounding_spec import SPEC_VERSION                    # noqa: E402
from prompts import load_prompt                                  # noqa: E402

DEFAULT_CORPUS = _REPO / "tests" / "gauntlet" / "corpus"
# Bundles are derived operator evidence: they live outside the frozen corpus.
DEFAULT_BUNDLES_ROOT = _REPO / "instance" / "corpus-bundles"

# The design's consecutive-runs rule: a case counts as demonstrated only when
# it meets its expectation in this many independent consecutive runs.
DEMONSTRATION_RUNS = 3

_CLASS_RE = re.compile(r"\*\*Class\*\*:\s*(.+)")
_LAYER_RE = re.compile(r"\*\*Expected catching layer\*\*:\s*(.+)")

_LAYER_JUDGES = {"truth": "truth", "consistency": "consistency",
                 "writing": "writing", "stage zero": "stage-zero"}


def real_judges():
    from adapters.models.claude_code import ClaudeCodeAdapter
    from adapters.models.codex_cli import CodexCliAdapter

    claude = ClaudeCodeAdapter()
    return {"truth": CodexCliAdapter(), "consistency": claude, "writing": claude}


def parse_case_md(case_dir: Path) -> dict:
    text = (case_dir / "case.md").read_text()
    declared_class = (_CLASS_RE.search(text) or [None, "unknown"])[1].strip()
    layer_line = (_LAYER_RE.search(text) or [None, ""])[1].lower()
    layer = next((v for k, v in _LAYER_JUDGES.items() if k in layer_line), None)
    clean = declared_class.startswith("clean") or "none" in layer_line
    return {"class": declared_class, "clean": clean, "layer": layer}


# -- frozen-corpus integrity ---------------------------------------------------
#
# The corpus is a frozen, blind-authored lane: its claim rests on the case
# content being exactly what was authored before any judge saw it. The freeze
# is a committed sha256 manifest over every case input; the harness verifies
# it before it uses a bundle and before EVERY demonstration leg, and the
# bundle carries the hashes of the exact inputs it was built from, so a stale
# or substituted bundle is a hard refusal rather than a silent pass.

MANIFEST_NAME = "CORPUS_MANIFEST.sha256"

# The frozen input file types the manifest covers (bundles are derived, not
# frozen, and are excluded from the manifest).
FROZEN_SUFFIXES = (".json", ".md")


class CorpusIntegrityError(RuntimeError):
    """The frozen corpus, or a bundle's binding to it, does not verify."""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(corpus_dir: Path) -> dict[str, str]:
    """The committed freeze: `sha256sum` output over every frozen case input,
    paths relative to the corpus root."""
    path = corpus_dir / MANIFEST_NAME
    if not path.is_file():
        raise CorpusIntegrityError(
            f"frozen-corpus manifest {MANIFEST_NAME} is missing from"
            f" {corpus_dir}; the demonstration cannot attest what it ran")
    manifest: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        if len(digest) != 64 or not name:
            raise CorpusIntegrityError(f"malformed manifest line: {line!r}")
        manifest[name.lstrip("./")] = digest
    if not manifest:
        raise CorpusIntegrityError(f"frozen-corpus manifest {path} is empty")
    return manifest


def manifest_cases(manifest: dict[str, str]) -> set[str]:
    """The case set the freeze DECLARES (root-level files such as the corpus
    README are not cases)."""
    return {name.split("/", 1)[0] for name in manifest if "/" in name}


def reconcile_corpus(corpus_dir: Path,
                     manifest: dict[str, str] | None = None) -> list[str]:
    """The discovered case directories against the declared case set, plus
    every declared case's frozen inputs. Enumerating what happens to be on
    disk certifies nothing: deleting a broken case would let the survivors run
    clean and certify the complete corpus. Returns the reasons, empty when the
    corpus reconciles exactly."""
    manifest = load_manifest(corpus_dir) if manifest is None else manifest
    declared = manifest_cases(manifest)
    discovered = {d.name for d in corpus_dir.iterdir() if d.is_dir()}
    reasons = []
    for name in sorted(declared - discovered):
        reasons.append(
            f"case '{name}' is declared in {MANIFEST_NAME} but its directory is"
            " absent; the corpus is incomplete and certifies nothing")
    for name in sorted(discovered - declared):
        reasons.append(
            f"case directory '{name}' is present but undeclared in"
            f" {MANIFEST_NAME}; only frozen cases may run")
    for name in sorted(declared & discovered):
        try:
            verify_case_integrity(corpus_dir / name, corpus_dir, manifest)
        except CorpusIntegrityError as e:
            reasons.append(str(e))
    return reasons


def frozen_inputs(case_dir: Path) -> dict[str, str]:
    """{relative path: sha256} over one case's frozen inputs on disk (the
    derived bundle is excluded)."""
    result = {}
    for path in sorted(case_dir.rglob("*")):
        rel = path.relative_to(case_dir)
        if (path.is_file() and "bundle" not in rel.parts
                and path.suffix in FROZEN_SUFFIXES):
            result[rel.as_posix()] = _sha_bytes(path.read_bytes())
    return result


def verify_case_integrity(case_dir: Path, corpus_dir: Path,
                          manifest: dict[str, str] | None = None) -> dict[str, str]:
    """Every frozen input of this case, both directions, against the freeze.
    Returns the verified {relative path: sha256} map for bundle binding."""
    manifest = load_manifest(corpus_dir) if manifest is None else manifest
    prefix = f"{case_dir.name}/"
    expected = {name[len(prefix):]: digest for name, digest in manifest.items()
                if name.startswith(prefix)}
    if not expected:
        raise CorpusIntegrityError(
            f"case '{case_dir.name}' has no entries in {MANIFEST_NAME};"
            " an unfrozen case never demonstrates anything")
    observed = frozen_inputs(case_dir)
    for name in sorted(set(expected) | set(observed)):
        if name not in observed:
            raise CorpusIntegrityError(
                f"{case_dir.name}/{name} is in the freeze but missing on disk")
        if name not in expected:
            raise CorpusIntegrityError(
                f"{case_dir.name}/{name} is not in the freeze (an input added"
                " after corpus freeze)")
        if observed[name] != expected[name]:
            raise CorpusIntegrityError(
                f"{case_dir.name}/{name} does not match the freeze:"
                f" {observed[name]} vs {expected[name]}")
    return observed


# Every derived bundle object that is not re-derived from a frozen input,
# with the hashes.json key recording it. Verified by reading the bytes back,
# so a swapped artifact fails.
HASHED_BUNDLE_OBJECTS = {
    "context.json": "input_context_hash",
    "cv.pdf": "artifact_hash",
    "extracted.txt": "extracted_text_hash",
    "verifier_report.json": "verifier_report_hash",
    "ats_report.json": "ats_report_hash",
    "policy_snapshot.json": "policy_snapshot_hash",
}


def derive_context_json(frozen_snapshot_bytes: bytes) -> str:
    """The permitted mechanical derivation of a frozen context snapshot, the
    ONLY transformation build_corpus_bundle.py applies: align
    normalization_spec_version to the shipped SPEC_VERSION (a stated
    placeholder in the frozen corpus), then serialize canonically. It is fully
    deterministic, so the harness re-derives it and requires equality; nothing
    is excluded from the comparison."""
    snapshot = json.loads(frozen_snapshot_bytes)
    snapshot["normalization_spec_version"] = SPEC_VERSION
    return json.dumps(snapshot, indent=2, sort_keys=True)


def verify_bundle(case_dir: Path, bundle: Path,
                  verified_inputs: dict[str, str]) -> None:
    """A bundle is evidence about this case only if its DERIVED OBJECTS are
    what the frozen inputs derive to. The bundle's own frozen_inputs claim is
    checked too, but it proves nothing on its own: an edited bundle can claim
    correct provenance while seeding a different self-consistent package, so
    the content model and context are re-derived here from the frozen files
    and required to match byte for byte, and every remaining derived object is
    read back and re-hashed against the bundle's record."""
    try:
        hashes = json.loads((bundle / "hashes.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CorpusIntegrityError(f"bundle hashes.json is unreadable: {e}") from e

    # 1. The claimed binding: names the exact frozen inputs, all of them.
    recorded = hashes.get("frozen_inputs")
    if not isinstance(recorded, dict) or not recorded:
        raise CorpusIntegrityError(
            f"bundle {bundle} records no frozen_inputs; rebuild it with"
            " scripts/build_corpus_bundle.py (a bundle that does not name its"
            " inputs cannot be bound to the freeze)")
    for name, digest in sorted(recorded.items()):
        if verified_inputs.get(name) != digest:
            raise CorpusIntegrityError(
                f"bundle {bundle} was built from a different"
                f" {name} ({digest}) than the frozen input"
                f" ({verified_inputs.get(name)}); it is stale or substituted")
    missing = sorted(set(verified_inputs) - set(recorded))
    if missing:
        raise CorpusIntegrityError(
            f"bundle {bundle} does not record the frozen inputs {missing}")

    # 2. The content model actually seeded, against the frozen one under
    # canonical serialization (never the claimed hash).
    try:
        frozen_cv = parse_cv_model((case_dir / "content_model.json").read_text())
        bundle_cv = parse_cv_model((bundle / "content_model.json").read_text())
    except Exception as e:
        raise CorpusIntegrityError(
            f"bundle {bundle} content model is unreadable: {type(e).__name__}:"
            f" {e}") from e
    if bundle_cv.to_json() != frozen_cv.to_json():
        raise CorpusIntegrityError(
            f"bundle {bundle} content model is not the frozen"
            f" {case_dir.name}/content_model.json; the bundle would seed a"
            " different package than the case")

    # 3. The context actually seeded, against the permitted derivation.
    expected_context = derive_context_json(
        (case_dir / "context_snapshot.json").read_bytes())
    try:
        actual_context = (bundle / "context.json").read_text()
    except OSError as e:
        raise CorpusIntegrityError(f"bundle context.json is unreadable: {e}") from e
    if actual_context != expected_context:
        raise CorpusIntegrityError(
            f"bundle {bundle} context.json is not the mechanical derivation of"
            f" the frozen {case_dir.name}/context_snapshot.json")

    # 4. Every remaining derived object, read back and re-hashed.
    for name, key in sorted(HASHED_BUNDLE_OBJECTS.items()):
        digest = hashes.get(key)
        if not isinstance(digest, str) or len(digest) != 64:
            raise CorpusIntegrityError(
                f"bundle {bundle} records no {key} for {name}; rebuild it with"
                " scripts/build_corpus_bundle.py")
        path = bundle / name
        if not path.is_file():
            raise CorpusIntegrityError(f"bundle {bundle} is missing {name}")
        if _sha_bytes(path.read_bytes()) != digest:
            raise CorpusIntegrityError(
                f"bundle {bundle} object {name} does not match its recorded"
                f" {key}; it was swapped or edited after the bundle was built")


def bundle_dir(case_dir: Path, corpus_dir: Path,
               bundles_root: Path | None = None) -> Path:
    """Where this case's completed bundle lives. Bundles are derived operator
    evidence and resolve ONLY from an explicit external root: a bundle inside
    the corpus tree sits in the frozen lane but outside manifest coverage, so
    it could be introduced or altered without invalidating anything and then
    consumed as evidence."""
    root = DEFAULT_BUNDLES_ROOT if bundles_root is None else bundles_root
    resolved = (root / case_dir.name).resolve()
    corpus = corpus_dir.resolve()
    if resolved == corpus or corpus in resolved.parents:
        raise CorpusIntegrityError(
            f"refusing a bundle inside the frozen corpus tree: {resolved}."
            " Bundles are derived evidence outside manifest coverage; build"
            f" them under {DEFAULT_BUNDLES_ROOT} (or another external root)"
            " and pass --bundles")
    return resolved


def broken_elements(case_dir: Path, corpus_dir: Path) -> set[str]:
    """The paired-counterfactual property, used mechanically: the accused
    element set is exactly where this case's content model differs from
    clean-base."""
    base = element_texts(parse_cv_model(
        (corpus_dir / "clean-base" / "content_model.json").read_text()), {})
    case = element_texts(parse_cv_model(
        (case_dir / "content_model.json").read_text()), {})
    return {k for k in set(base) | set(case) if base.get(k) != case.get(k)}


def seed_bundle(instance: Path, bundle: Path) -> tuple[sqlite3.Connection, str]:
    """One VERIFIED version whose audit bundle is the completed corpus
    bundle, seeded through the repository's own lifecycle path."""
    db = instance / "open-career.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    hashes = json.loads((bundle / "hashes.json").read_text())
    with conn:
        conn.execute("INSERT INTO role_families (id, name, rationale)"
                     " VALUES ('rf_demo', 'demo', 'corpus demonstration')")
    storage = LocalStorageAdapter(instance)
    storage.write_bytes_new("corpus/context.json",
                            (bundle / "context.json").read_bytes())
    storage.write_bytes_new("corpus/cv.pdf", (bundle / "cv.pdf").read_bytes())
    repo = SqlitePackageRepository(conn)
    package = repo.get_or_create_base_package("rf_demo")
    version = repo.reserve_version(package.id, "corpus-seeder", 60)
    repo.finalize_verified(
        version.id, "corpus-seeder", 1,
        content_model_json=(bundle / "content_model.json").read_text(),
        context_snapshot_locator="corpus/context.json",
        input_context_hash=hashes["input_context_hash"],
        verifier_report_json=(bundle / "verifier_report.json").read_text(),
        ats_report_json=(bundle / "ats_report.json").read_text(),
        artifact_locator="corpus/cv.pdf",
        artifact_hash=hashes["artifact_hash"])
    return conn, version.id


def heartbeat_repo_factory(db_path: Path):
    """The heartbeat thread's repository: a dedicated connection to the same
    database file, opened inside the thread (sqlite connections are
    single-thread by default)."""

    @contextmanager
    def factory():
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield SqlitePackageRepository(conn)
        finally:
            conn.close()

    return factory


def run_case(case_dir: Path, corpus_dir: Path, judges, workdir: Path,
             extractor=None, bundles_root: Path | None = None) -> dict:
    """One case through the real runner; returns the machine-readable
    per-case record. extractor is injectable for harness self-tests only.

    Frozen-corpus integrity is verified HERE, so it is checked before the
    bundle is used and before every demonstration leg (a trial leg is one
    run_case call in a fresh subprocess), never once at the top of a run."""
    expectation = parse_case_md(case_dir)
    try:
        bundle = bundle_dir(case_dir, corpus_dir, bundles_root)
        verified_inputs = verify_case_integrity(case_dir, corpus_dir)
        if bundle.is_dir():
            verify_bundle(case_dir, bundle, verified_inputs)
    except CorpusIntegrityError as e:
        return {"case": case_dir.name, **expectation,
                "status": "corpus-integrity", "ok": False, "detail": str(e)}
    if not bundle.is_dir():
        return {"case": case_dir.name, **expectation, "status": "no-bundle",
                "ok": False,
                "detail": f"complete the bundle first at {bundle}"
                          " (scripts/build_corpus_bundle.py)"}
    instance = workdir / case_dir.name
    instance.mkdir(parents=True)
    conn, version_id = seed_bundle(instance, bundle)
    try:
        repo = SqlitePackageRepository(conn)
        policy_snapshot = json.loads((bundle / "policy_snapshot.json").read_text())
        profile = {
            "authorized_in_country":
                policy_snapshot["work_authorization"]["authorized_in_country"],
            "needs_sponsorship":
                policy_snapshot["work_authorization"]["needs_sponsorship"],
            "country": policy_snapshot["work_authorization"]["country"],
        }
        runner = GauntletRunner(
            repo, LocalStorageAdapter(instance),
            extractor or PopplerPdfTextExtractor(),
            judges, {j: load_prompt(PROMPT_FILES[j]) for j in JUDGES},
            # The heartbeat renews from its own thread, so it gets its own
            # connection to the same database file: sharing this connection
            # would turn every leg outliving one heartbeat interval (every
            # real model-backed leg) into an infrastructure failure.
            heartbeat_repo_factory=heartbeat_repo_factory(
                instance / "open-career.sqlite3"))
        result = runner.run(repo.get_version(version_id), profile,
                            {"never_render": policy_snapshot["never_render"]})
        if result.run is None:
            return {"case": case_dir.name, **expectation, "status": "no-run",
                    "ok": False, "detail": result.detail}
        report = json.loads(result.run.report_json)
        record = {
            "case": case_dir.name, **expectation,
            "status": "ran", "verdict": report["verdict"],
            "stop_reason": report["stop_reason"],
            "resolved_models": report["resolved_models"],
            "provider_versions": report.get("provider_versions", {}),
            "model_set_status": report["model_set_status"],
            "failed_invariants": [r["rule"] for r in report["invariants"]
                                  if r["disposition"] == "fail"],
            "blocking_findings": {
                j["judge"]: [f["element_id"] for f in j["findings"]
                             if f["severity"] == "blocking"]
                for j in report["judges"]},
        }
        record["ok"] = _check_expectation(record, case_dir, corpus_dir)
        return record
    finally:
        conn.close()


# For a stage-zero case, WHICH deterministic rule must fire. Any other
# invariant failing is an unrelated failure, never a catch: certifying
# wrong-work-authorization on an audit-integrity or date-coherence FAIL would
# claim evidence the suite never produced.
_CLASS_INVARIANT = {"wrong-work-authorization": "work-authorization"}


def _catcher_hit(record: dict, case_dir: Path, corpus_dir: Path) -> bool:
    """Did the case's DECLARED catching layer produce the expected valid
    evidence (the case's named stage-zero rule failing, or a valid blocking
    finding citing the broken element)? Unrelated failures do not count."""
    if record["layer"] == "stage-zero":
        rule = _CLASS_INVARIANT.get(record.get("class"))
        if rule is None:
            # No declared rule, no attestable catch.
            return False
        return rule in (record.get("failed_invariants") or [])
    expected_elements = broken_elements(case_dir, corpus_dir)
    accused = set(record.get("blocking_findings", {}).get(record["layer"] or "", []))
    return bool(accused & expected_elements)


def _check_expectation(record: dict, case_dir: Path, corpus_dir: Path) -> bool:
    if record["clean"]:
        return record["verdict"] == "PASS"
    return record["verdict"] == "FAIL" and _catcher_hit(record, case_dir, corpus_dir)


UNREPORTED = "unreported"


def validate_expected_models(expected_models,
                             expected_provider_versions=None) -> str | None:
    """The expected identity set is MANDATORY (the table's whole claim names
    it): a mapping over exactly the configured judge set.

    A judge whose backend genuinely reports no resolved model may be expected
    as the literal 'unreported' (the Codex CLI is permanently in that state:
    its `codex exec --json` stream carries no model field). That is not a
    waiver: such a judge MUST also carry an expected provider version, so the
    pinned identity is the pair (unreported model, provider version) and a
    change in either half voids the table. Returns the problem, or None."""
    if not isinstance(expected_models, dict):
        return ("expected_models is mandatory: pass the recorded table's"
                f" {{judge: model}} mapping over the judge set {sorted(JUDGES)}")
    if set(expected_models) != set(JUDGES):
        return (f"expected_models keys {sorted(expected_models)} do not match"
                f" the configured judge set {sorted(JUDGES)}")
    bad = [j for j, m in expected_models.items()
           if not isinstance(m, str) or not m.strip()]
    if bad:
        return f"expected_models values for {sorted(bad)} are not model identities"
    versions = expected_provider_versions or {}
    if not isinstance(versions, dict):
        return "expected_provider_versions must be a {judge: version} mapping"
    unknown = set(versions) - set(JUDGES)
    if unknown:
        return (f"expected_provider_versions names non-judges {sorted(unknown)};"
                f" the judge set is {sorted(JUDGES)}")
    needs_version = sorted(j for j, m in expected_models.items() if m == UNREPORTED)
    missing = [j for j in needs_version
               if not isinstance(versions.get(j), str) or not versions[j].strip()
               or versions[j] == "unavailable"]
    if missing:
        return (f"judges {sorted(missing)} are expected to report no model"
                " identity, so the table must pin their provider version"
                " instead: pass expected_provider_versions for them")
    return None


def _matches_expected_provider_versions(record: dict, expected_versions) -> list[str]:
    """Every pinned provider version against what the run observed. Returns
    the mismatches, empty when they agree."""
    observed = record.get("provider_versions") or {}
    return [f"{judge}: observed provider version"
            f" {observed.get(judge, 'absent')!r} does not match the expected"
            f" {version!r}"
            for judge, version in sorted((expected_versions or {}).items())
            if observed.get(judge) != version]


def limitation_lines(record: dict) -> list[str]:
    """The verbatim limitation text for the published table, for every judge
    whose backend reported no model identity. The table must never read as
    though the model were known."""
    lines = []
    versions = record.get("provider_versions") or {}
    for judge, models in sorted((record.get("resolved_models") or {}).items()):
        if UNREPORTED not in models:
            continue
        version = versions.get(judge, "unavailable")
        lines.append(
            f"Model identity limitation ({judge} judge): the backend reported"
            " no resolved model identity, so the run records model:"
            " unreported. For the Truth Judge this is the Codex CLI, whose"
            " `codex exec --json` event stream carries no model field. The"
            f" demonstrated claim for this judge is bounded by the observed"
            f" provider version {version!r}, not by a model identity, and a"
            " change in that provider version voids this table until"
            " re-demonstrated.")
    return lines


def _matches_expected_models(record: dict, expected_models: dict) -> bool:
    """Every completed run must match the expected set exactly, else the
    record is non-demonstrating (an observed model-set change voids the
    table without a suite bump)."""
    observed = {judge: set(models)
                for judge, models in record.get("resolved_models", {}).items()
                if models}
    return observed == {judge: {model} for judge, model in expected_models.items()}


def run_demonstration(corpus_dir: Path, judges, only_case: str | None = None,
                      expected_models: dict | None = None,
                      extractor=None, bundles_root: Path | None = None,
                      expected_provider_versions: dict | None = None) -> dict:
    cases = sorted(d for d in corpus_dir.iterdir()
                   if d.is_dir() and (only_case is None or d.name == only_case))
    reasons = []
    try:
        manifest = load_manifest(corpus_dir)
        # The corpus is reconciled against its freeze BEFORE any leg runs: the
        # declared case set must match the discovered directories exactly, and
        # every declared case's frozen inputs must verify.
        corpus_reasons = reconcile_corpus(corpus_dir, manifest)
    except CorpusIntegrityError as e:
        corpus_reasons = [str(e)]
    if only_case is not None:
        # A single-case run is a debugging aid, not a certification: the legs
        # still run, the record can never be demonstrating.
        reasons.append(f"only case '{only_case}' ran; a partial run never"
                       " certifies the corpus")
    if corpus_reasons:
        # No verified freeze, no demonstration: the record cannot attest what
        # it ran, and no leg runs.
        return {
            "suite_version": SUITE_VERSION,
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "corpus": str(corpus_dir),
            "runs_per_case": DEMONSTRATION_RUNS,
            "expected_models": expected_models,
            "expected_provider_versions": expected_provider_versions,
            "observed_model_sets": [], "observed_provider_versions": [],
            "limitations": [], "demonstrating": False,
            "non_demonstrating_reasons": [f"frozen-corpus integrity: {r}"
                                          for r in corpus_reasons],
            "cases": [],
        }
    workdir = Path(tempfile.mkdtemp(prefix="gauntlet-demo-"))
    try:
        # The design's consecutive-runs rule: a case counts as demonstrated
        # only when it meets its expectation in DEMONSTRATION_RUNS independent
        # consecutive runs. Judge determinism is statistical, so a single pass
        # says nothing; all legs are persisted in the record.
        cases_legs = [
            [run_case(case, corpus_dir, judges, workdir / f"leg{i}",
                      extractor=extractor, bundles_root=bundles_root)
             for i in range(DEMONSTRATION_RUNS)]
            for case in cases]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    expected_problem = validate_expected_models(expected_models,
                                                expected_provider_versions)
    if expected_problem:
        reasons.append(expected_problem)
    results = []
    for legs in cases_legs:
        name = legs[0]["case"]
        for i, record in enumerate(legs, start=1):
            where = f"{name} (run {i} of {DEMONSTRATION_RUNS})"
            if record.get("status") != "ran":
                reasons.append(f"{where}: {record.get('status')}"
                               f" ({record.get('detail', '')})".rstrip(" ()"))
                continue
            status = record.get("model_set_status")
            # 'unreported' is the honest, permanent state of a backend that
            # reports no resolved model; the expectation check below is what
            # decides whether that was the pinned identity. 'mixed' is always
            # non-demonstrating, and a run with no model calls is expected
            # only for a stage-zero case.
            if status == "mixed":
                reasons.append(f"{where}: model set is mixed")
            elif status == "no-model-calls" and record.get("layer") != "stage-zero":
                reasons.append(f"{where}: no judge made a model call")
            if expected_problem is None:
                matched = _matches_expected_models(record, expected_models)
                version_problems = _matches_expected_provider_versions(
                    record, expected_provider_versions)
                record["model_set_matches_expected"] = matched
                record["provider_versions_match_expected"] = not version_problems
                if not matched:
                    reasons.append(
                        f"{where}: observed models"
                        f" {record.get('resolved_models')} do not match the"
                        f" expected set {expected_models}; the table is void"
                        " until re-demonstrated")
                for problem in version_problems:
                    reasons.append(
                        f"{where}: {problem}; the table is void until"
                        " re-demonstrated")
            if not record.get("ok"):
                reasons.append(f"{where}: expectation not met")
        results.append({
            "case": name,
            "class": legs[0].get("class"), "clean": legs[0].get("clean"),
            "layer": legs[0].get("layer"),
            "runs_required": DEMONSTRATION_RUNS,
            # The per-leg outcomes, stated: three consecutive passes are the
            # claim, so a two-of-three case is visibly not demonstrated.
            "leg_outcomes": [{"run": i, "status": leg.get("status"),
                              "verdict": leg.get("verdict"),
                              "ok": bool(leg.get("ok"))}
                             for i, leg in enumerate(legs, start=1)],
            "ok": all(leg.get("ok") for leg in legs),
            "limitations": sorted({line for leg in legs
                                   for line in limitation_lines(leg)}),
            "legs": legs,
        })
    ran_legs = [leg for legs in cases_legs for leg in legs
                if leg.get("status") == "ran"]
    model_sets = sorted({json.dumps(leg.get("resolved_models", {}), sort_keys=True)
                         for leg in ran_legs})
    provider_sets = sorted({json.dumps(leg.get("provider_versions", {}),
                                       sort_keys=True) for leg in ran_legs})
    # The limitation text travels IN the record, verbatim, for the published
    # table: a judge whose backend reports no model identity must never read
    # as though its model were known.
    limitations = sorted({line for leg in ran_legs
                          for line in limitation_lines(leg)})
    return {
        "suite_version": SUITE_VERSION,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus": str(corpus_dir),
        "runs_per_case": DEMONSTRATION_RUNS,
        "expected_models": expected_models,
        "expected_provider_versions": expected_provider_versions,
        "observed_model_sets": [json.loads(s) for s in model_sets],
        "observed_provider_versions": [json.loads(s) for s in provider_sets],
        "limitations": limitations,
        "demonstrating": not reasons,
        "non_demonstrating_reasons": reasons,
        "cases": results,
    }


# -- discrimination protocol (automatable operations) -------------------------

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cp_backup(target: Path, workdir: Path) -> tuple[Path, str]:
    """cp backup with its hash recorded (never `git checkout` to restore)."""
    backup = workdir / (target.name + ".bak")
    shutil.copyfile(target, backup)
    return backup, _sha(backup)


def apply_mutation(target: Path, old: str, new: str) -> str:
    """A named rule-disabling mutation, recorded as the exact substitution."""
    source = target.read_text()
    if old not in source:
        raise ValueError(f"mutation target text not found in {target}")
    target.write_text(source.replace(old, new, 1))
    return f"--- replaced ---\n{old}\n--- with ---\n{new}"


def restore_verified(backup: Path, backup_hash: str, target: Path) -> None:
    """Byte-identical cp restore, hash-verified on both ends."""
    if _sha(backup) != backup_hash:
        raise ValueError("backup no longer matches its recorded hash; abort")
    shutil.copyfile(backup, target)
    if _sha(target) != backup_hash:
        raise ValueError("restore is not byte-identical; abort")


# Predeclared discrimination mutations, keyed by case class: which checker
# file the trial may touch and the exact named rule-disabling substitution.
# Arbitrary targets are rejected; the registry is validated before any edit.
#
# A judge-layer mutation must remove the declared catcher's valid blocking
# EVIDENCE from the report, not merely the verdict mapping: the catcher check
# reads the findings themselves, so disabling only the blocking-to-FAIL
# mapping would leave the mutated leg counting as a catch and no judge-layer
# trial could discriminate. Each judge-layer mutation therefore disables that
# one judge's finding admission, so its findings never reach the report.
#
# The mutation drops the parsed findings rather than routing them into the
# invalid list: an all-invalid finding set is an OPERATIONAL_ABSTAIN by
# design, which makes the leg an INVALID trial instead of an escape.
_JUDGE_ADMISSION_ANCHOR = (
    "        try:\n"
    "            valid, invalid = validate_findings(judge, findings, cv, profile)")


def _discard_judge_findings(judge: str) -> dict:
    return {
        "target": "packages/domain/gauntlet_judges.py",
        "name": f"discard-{judge}-judge-finding-admission",
        "old": _JUDGE_ADMISSION_ANCHOR,
        "new": (_JUDGE_ADMISSION_ANCHOR
                + f'\n            if judge == "{judge}":\n'
                  "                findings, valid, invalid = [], (), ()"),
    }


# Which judge is the declared catching layer for each broken class.
_CLASS_JUDGE = {
    "fabricated-claim": "truth", "invented-metric": "truth",
    "scope-inflation-semantic": "truth", "negation-reversal": "truth",
    "attribution-swap": "truth", "false-causality": "truth",
    "misleading-aggregation": "truth",
    "inconsistent-date": "consistency",
    "cross-section-contradiction": "consistency",
    "generic-writing": "writing",
}

MUTATION_REGISTRY: dict[str, dict] = {
    "wrong-work-authorization": {
        "target": "packages/domain/gauntlet_invariants.py",
        "name": "disable-work-authorization-fail",
        "old": '    if problems:\n        return InvariantResult'
               '("work-authorization", FAIL, "; ".join(problems))',
        "new": '    if False:\n        return InvariantResult'
               '("work-authorization", FAIL, "; ".join(problems))',
    },
    **{cls: _discard_judge_findings(judge)
       for cls, judge in _CLASS_JUDGE.items()},
}


def _registry_entry(case_dir: Path, registry: dict) -> tuple[dict, Path]:
    """The checked registry lookup: the case class must have a predeclared
    entry, the target must exist, and the named mutation must apply."""
    declared_class = parse_case_md(case_dir)["class"]
    entry = registry.get(declared_class)
    if entry is None:
        raise ValueError(
            f"no predeclared discrimination mutation for class"
            f" '{declared_class}'; arbitrary targets are rejected")
    target = Path(entry["target"])
    if not target.is_absolute():
        target = _REPO / target
    if not target.is_file():
        raise ValueError(f"registry target does not exist: {target}")
    if entry["old"] not in target.read_text():
        raise ValueError(
            f"registry mutation '{entry['name']}' no longer applies to"
            f" {target}; update the registry before running trials")
    return entry, target


def _default_leg_spawner(case_dir: Path, corpus_dir: Path, workdir: Path,
                         bundles_root: Path | None = None) -> dict:
    """One trial leg in a FRESH subprocess, so imports reflect the on-disk
    (mutated or restored) source, never this process's cached modules."""
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "leg.json"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "_leg",
         "--case-dir", str(case_dir), "--corpus", str(corpus_dir),
         "--out", str(out)]
        + (["--bundles", str(bundles_root)] if bundles_root else []),
        capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not out.exists():
        return {"case": case_dir.name, "status": "error",
                "layer": parse_case_md(case_dir)["layer"],
                "detail": (proc.stderr or proc.stdout).strip()[-500:]}
    return json.loads(out.read_text())


def _trial_run_validity(record: dict) -> tuple[bool, str]:
    """A trial leg is valid only when the run COMPLETED normally: it ran, it
    reached a terminal verdict, and (for judge-layer cases) no unrelated
    stage-zero failure decided it. Infrastructure or model failures make the
    trial invalid, never an escape."""
    if record.get("status") != "ran":
        return False, f"run did not complete: {record.get('status')}" \
                      f" ({record.get('detail', '')})".strip()
    if record.get("verdict") == "INCOMPLETE":
        return False, "attempt incomplete (operational abstention)"
    if record["layer"] != "stage-zero" and record.get("failed_invariants"):
        return False, ("unrelated stage-zero failure decided the run:"
                       f" {record['failed_invariants']}")
    return True, ""


def run_discrimination_trial(case_dir: Path, corpus_dir: Path,
                             registry: dict | None = None,
                             leg_spawner=None,
                             bundles_root: Path | None = None) -> dict:
    """The predeclared protocol for one case, driven ONLY by the checked
    mutation registry (case class -> checker target + named rule-disabling
    mutation): backup, disable the rule (recording the diff), case must
    ESCAPE (the mutated run completes normally and the declared catcher no
    longer emits its valid blocking evidence), byte-identical restore
    verified by hash, case must be CAUGHT. Each leg runs in a fresh
    subprocess so imports reflect the on-disk source. A crashed or
    abstaining leg records an INVALID trial, never an escape."""
    entry, target = _registry_entry(case_dir, registry or MUTATION_REGISTRY)
    spawn = leg_spawner or (
        lambda case, corpus, work: _default_leg_spawner(
            case, corpus, work, bundles_root=bundles_root))
    workdir = Path(tempfile.mkdtemp(prefix="gauntlet-trial-"))
    try:
        backup, backup_hash = cp_backup(target, workdir)
        diff = apply_mutation(target, entry["old"], entry["new"])
        try:
            try:
                mutated = spawn(case_dir, corpus_dir, workdir / "mutated")
            except Exception as e:  # a crash is an invalid trial, not an escape
                mutated = {"case": case_dir.name, "status": "error",
                           "layer": parse_case_md(case_dir)["layer"],
                           "detail": f"{type(e).__name__}: {e}"}
        finally:
            restore_verified(backup, backup_hash, target)
        try:
            restored = spawn(case_dir, corpus_dir, workdir / "restored")
        except Exception as e:
            restored = {"case": case_dir.name, "status": "error",
                        "layer": parse_case_md(case_dir)["layer"],
                        "detail": f"{type(e).__name__}: {e}"}
        mutated_valid, mutated_reason = _trial_run_validity(mutated)
        restored_valid, restored_reason = _trial_run_validity(restored)
        valid = mutated_valid and restored_valid
        invalid_reason = "; ".join(r for r in (mutated_reason, restored_reason) if r)
        escaped = (mutated_valid
                   and not _catcher_hit(mutated, case_dir, corpus_dir))
        caught = (restored_valid
                  and bool(restored.get("ok", False)))
        return {
            "case": case_dir.name, "target": str(target),
            "mutation_name": entry["name"],
            "mutation_diff": diff, "backup_hash": backup_hash,
            "restore_verified": True,
            "mutated_run": mutated, "restored_run": restored,
            "valid_trial": valid, "invalid_reason": invalid_reason,
            "escaped_under_mutation": escaped,
            "caught_after_restore": caught,
            "discriminates": valid and escaped and caught,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run every completed corpus case once")
    p_run.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p_run.add_argument("--bundles", type=Path, default=None,
                       help="root holding the completed per-case bundles"
                            f" (default: {DEFAULT_BUNDLES_ROOT}, or an"
                            " existing in-case bundle)")
    p_run.add_argument("--case", default=None, help="run only this case")
    p_run.add_argument("--out", type=Path, default=Path("demonstration.json"))
    p_run.add_argument("--expected-provider-versions", default="{}",
                       help="JSON {judge: version} pinning each backend's own"
                            " version (mandatory for any judge expected to"
                            " report no model identity, e.g. the Codex-backed"
                            " Truth Judge); a change voids the table")
    p_run.add_argument("--expected-models", required=True,
                       help="JSON {judge: model} from the recorded"
                            " demonstration table (mandatory); any observed"
                            " deviation voids the record")
    p_disc = sub.add_parser(
        "discriminate",
        help="one registry-driven mutate/restore discrimination trial")
    p_disc.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p_disc.add_argument("--bundles", type=Path, default=None)
    p_disc.add_argument("--case", required=True)
    p_disc.add_argument("--out", type=Path, default=Path("trial.json"))
    # Internal: one trial leg in a fresh interpreter (never typed by hand).
    p_leg = sub.add_parser("_leg")
    p_leg.add_argument("--case-dir", type=Path, required=True)
    p_leg.add_argument("--corpus", type=Path, required=True)
    p_leg.add_argument("--bundles", type=Path, default=None)
    p_leg.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "_leg":
        workdir = Path(tempfile.mkdtemp(prefix="gauntlet-leg-"))
        try:
            record = run_case(args.case_dir, args.corpus, real_judges(), workdir,
                              bundles_root=args.bundles)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        args.out.write_text(json.dumps(record, indent=2, sort_keys=True))
        return
    if args.command == "run":
        record = run_demonstration(
            args.corpus, real_judges(), args.case,
            expected_models=json.loads(args.expected_models),
            bundles_root=args.bundles,
            expected_provider_versions=json.loads(args.expected_provider_versions))
    else:
        record = run_discrimination_trial(args.corpus / args.case, args.corpus,
                                          bundles_root=args.bundles)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True))
    print(f"record written to {args.out}")
    for line in record.get("limitations", []):
        print(f"  LIMITATION: {line}")
    for case in record.get("cases", [record]):
        name = case.get("case", "?")
        outcomes = case.get("leg_outcomes")
        detail = (" ".join(f"run{o['run']}={o['verdict'] or o['status']}"
                           f"/{'ok' if o['ok'] else 'not-ok'}" for o in outcomes)
                  if outcomes else
                  f"verdict={case.get('verdict', case.get('status', ''))}")
        print(f"  {name}: ok={case.get('ok', case.get('discriminates'))} {detail}")


if __name__ == "__main__":
    main()
