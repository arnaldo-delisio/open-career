"""Mechanical corpus-bundle completion for the Gauntlet regression corpus
(spec: the scope's decisions/gauntlet-design.md, "The regression corpus").

The blind corpus author writes only the case CONTENT (a context snapshot and
a content model pair); this utility, run by the independent test operator
after corpus freeze, completes the bundle mechanically through the real
pipeline pieces: aligns the snapshot's normalization_spec_version to the
shipped SPEC_VERSION (a stated placeholder in the frozen corpus), renders the
PDF with the real renderer, extracts its text with the real pdftotext path,
runs the real GroundingVerifier and ATS check, computes the recorded hashes,
and serializes a policy snapshot. Nothing here authors or edits case content.

The frozen corpus is READ-ONLY here: a bundle is derived operator evidence and
never lands inside the corpus tree. The bundle records the sha256 of the exact
frozen inputs it was built from, so the demonstration harness can refuse a
stale or substituted bundle instead of silently passing.

Usage:
  uv run python scripts/build_corpus_bundle.py <case_dir> [--out <dir>]
      [--profile-json '<json>'] [--policies-json '<json>'] [--pages N]

<case_dir> must hold context_snapshot.json and content_model.json. The bundle
lands in --out (default: instance/corpus-bundles/<case_dir name>):
context.json (canonical bytes), content_model.json, cv.pdf, extracted.txt,
verifier_report.json, ats_report.json, policy_snapshot.json, hashes.json.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.render.html_pdf import PlaywrightCvRenderer          # noqa: E402
from adapters.render.pdftext import PopplerPdfTextExtractor        # noqa: E402
from domain.ats_check import check_ats                             # noqa: E402
from domain.cv_model import parse_cv_model                         # noqa: E402
from domain.gauntlet_invariants import (                           # noqa: E402
    SnapshotContext,
    build_policy_snapshot,
    policy_snapshot_json,
)
from domain.grounding import GroundingVerifier                     # noqa: E402
from domain.grounding_spec import SPEC_VERSION                     # noqa: E402


_REPO = Path(__file__).resolve().parents[1]
FROZEN_CORPUS = _REPO / "tests" / "gauntlet" / "corpus"
DEFAULT_BUNDLES_ROOT = _REPO / "instance" / "corpus-bundles"

# The frozen input file types a bundle is built from and bound to (the same
# set the corpus freeze manifest covers; derived bundles are excluded).
FROZEN_SUFFIXES = (".json", ".md")
REQUIRED_INPUTS = ("context_snapshot.json", "content_model.json")


def frozen_input_hashes(case_dir: Path, out_dir: Path) -> dict[str, str]:
    """{relative path: sha256} over the case's frozen inputs, the binding the
    demonstration harness checks."""
    hashes = {}
    for path in sorted(case_dir.rglob("*")):
        rel = path.relative_to(case_dir)
        if (path.is_file() and "bundle" not in rel.parts
                and path.suffix in FROZEN_SUFFIXES
                and out_dir not in path.resolve().parents):
            hashes[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    missing = [name for name in REQUIRED_INPUTS if name not in hashes]
    if missing:
        raise ValueError(f"case {case_dir} is missing frozen inputs {missing}")
    return hashes


def check_out_dir(out_dir: Path) -> Path:
    """The frozen corpus is never written into: a bundle inside the corpus
    tree would put derived, operator-produced bytes in the blind-authored
    lane and break the freeze."""
    resolved = out_dir.resolve()
    if resolved == FROZEN_CORPUS or FROZEN_CORPUS in resolved.parents:
        raise ValueError(
            f"refusing to write into the frozen corpus tree: {resolved}."
            f" Bundles are derived evidence; use --out under"
            f" {DEFAULT_BUNDLES_ROOT} or another location outside"
            f" {FROZEN_CORPUS}")
    return resolved


def build_bundle(case_dir: Path, out_dir: Path, profile_overrides: dict,
                 policies: dict, page_budget: int) -> dict:
    out_dir = check_out_dir(out_dir)
    snapshot = json.loads((case_dir / "context_snapshot.json").read_text())
    # Mechanical alignment, stated in the corpus README: the blind author
    # could not read the shipped SPEC_VERSION.
    snapshot["normalization_spec_version"] = SPEC_VERSION
    snapshot_json = json.dumps(snapshot, indent=2, sort_keys=True)
    cv = parse_cv_model((case_dir / "content_model.json").read_text())

    verifier_report = GroundingVerifier(SnapshotContext(snapshot)).verify(cv)
    trail = json.dumps({"final": json.loads(verifier_report.to_json()),
                        "attempts": 1, "fallback_used": False, "dropped": []},
                       indent=2, sort_keys=True)

    pdf = PlaywrightCvRenderer().render_pdf(cv)
    extracted = PopplerPdfTextExtractor().extract_layout(pdf)
    ats_report = check_ats(extracted, cv, page_budget)

    profile = dict(snapshot["renderable_grounding_view"].get("profile", {}))
    profile.update(profile_overrides)
    policy_snapshot = policy_snapshot_json(build_policy_snapshot(profile, policies))

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "context.json").write_text(snapshot_json)
    (out_dir / "content_model.json").write_text(cv.to_json())
    (out_dir / "cv.pdf").write_bytes(pdf)
    (out_dir / "extracted.txt").write_text(extracted)
    (out_dir / "verifier_report.json").write_text(trail)
    (out_dir / "ats_report.json").write_text(ats_report.to_json())
    (out_dir / "policy_snapshot.json").write_text(policy_snapshot)
    # Every derived object gets a recorded hash, so the demonstration harness
    # can read the bytes back and refuse a swapped or edited bundle.
    hashes = {
        "input_context_hash": hashlib.sha256(snapshot_json.encode()).hexdigest(),
        "artifact_hash": hashlib.sha256(pdf).hexdigest(),
        "extracted_text_hash": hashlib.sha256(extracted.encode()).hexdigest(),
        "verifier_report_hash": hashlib.sha256(trail.encode()).hexdigest(),
        "ats_report_hash": hashlib.sha256(
            ats_report.to_json().encode()).hexdigest(),
        "policy_snapshot_hash": hashlib.sha256(policy_snapshot.encode()).hexdigest(),
        "verifier_passed": verifier_report.passed,
        "ats_passed": ats_report.passed,
        "spec_version": SPEC_VERSION,
        # The binding to the freeze: the exact case inputs this bundle was
        # built from. The harness refuses any bundle whose recorded input
        # hashes do not match the frozen files.
        "frozen_inputs": frozen_input_hashes(case_dir, out_dir),
    }
    (out_dir / "hashes.json").write_text(json.dumps(hashes, indent=2, sort_keys=True))
    return hashes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--out", type=Path, default=None,
                        help="bundle output directory (default:"
                             " instance/corpus-bundles/<case>); any path"
                             " inside the frozen corpus tree is refused")
    parser.add_argument("--profile-json", default="{}",
                        help="profile field overrides for the policy snapshot"
                             " projection (e.g. authorized_in_country)")
    parser.add_argument("--policies-json", default="{}",
                        help="policies for the snapshot (e.g. never_render)")
    parser.add_argument("--pages", type=int, default=1)
    args = parser.parse_args(argv)
    out_dir = args.out or (DEFAULT_BUNDLES_ROOT / args.case_dir.resolve().name)
    hashes = build_bundle(args.case_dir, out_dir,
                          json.loads(args.profile_json),
                          json.loads(args.policies_json), args.pages)
    print(f"bundle written to {out_dir}")
    for key, value in sorted(hashes.items()):
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
