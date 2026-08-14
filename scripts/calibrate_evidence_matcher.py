"""Calibrate the deterministic evidence-coverage matcher (OC-37 §5).

The design requires coverage to be computed by "normalized token match"
against the graph's capability names, with zero model involvement. Which
normalized token rule is a judgment call, and this harness is how it was made
evidence-driven rather than guessed: it regenerates a corpus of real
requirement-like sentences from the live raw payloads, scores candidate rules
over it, and reports each rule's agreement with a small hand-labelled sample
committed beside this script.

Instance data is gitignored, so nothing here depends on a committed fixture of
postings: the corpus is regenerated on demand and is deterministic given a
seed. The labels are committed (they carry their phrase text inline), so the
agreement numbers reproduce on any machine that can regenerate the corpus.

No model calls, ever: every rule here is pure Python over normalized tokens.

Usage:
    uv run python scripts/calibrate_evidence_matcher.py corpus [--seed 7]
    uv run python scripts/calibrate_evidence_matcher.py pairs [--seed 7]
    uv run python scripts/calibrate_evidence_matcher.py evaluate [--seed 7]
"""

import argparse
import html
import json
import math
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))

from adapters.sources import build_adapters  # noqa: E402
from adapters.storage.instance import db_path, instance_dir  # noqa: E402
from domain.requirements import (  # noqa: E402
    CONTENT_FRACTION_BP, matched_requirements, normalized_tokens,
    stopword_free_tokens,
)

def read_only_connection() -> sqlite3.Connection:
    """A strictly read-only handle on the live instance database. The
    application's own connect() would open read-write and set WAL journal
    mode, which writes the database header and the -wal/-shm files: a
    measurement harness must not touch the data it measures (Codex r1).
    mode=ro also refuses to create a database that is not there. Reading a
    WAL database still attaches the shared-memory index, so the -shm/-wal
    handles may appear: that is the normal cost of any reader and leaves the
    database bytes untouched (verified by hashing before and after a run)."""
    return sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)


LABELS_PATH = Path(__file__).resolve().parent / "evidence_matcher_labels.json"

# A requirement-like sentence: the extraction stage proposes requirement
# phrases from posting text, so the corpus proxies them with the posting
# sentences that state a requirement. Cue-based, deliberately broad.
_CUES = re.compile(
    r"\b(experience|experienced|ability|able to|skills?|proficien\w*|knowledge|"
    r"familiar\w*|understanding|expertise|track record|background in|"
    r"you will|you'll|responsible for|comfortable|demonstrated|strong|"
    r"required|requirements?|must have|nice to have|degree|years)\b",
    re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SENTENCE_SPLIT = re.compile(r"(?<=[.;:!?])\s+|\n+")
_WS = re.compile(r"\s+")


def _sentences(description: str) -> list[str]:
    # Vendors ship HTML, and some ship it entity-escaped inside HTML, so the
    # unescape/strip pair runs twice before sentences are cut.
    text = description or ""
    for _ in range(2):
        text = _TAG.sub("\n", html.unescape(text))
    out = []
    for chunk in _SENTENCE_SPLIT.split(text):
        phrase = _WS.sub(" ", chunk).strip(" \t-*•·")
        words = phrase.split()
        if not (4 <= len(words) <= 30):
            continue
        if not _CUES.search(phrase):
            continue
        out.append(phrase)
    return out


def _read_pages(storage_root: Path, locator: str) -> list:
    stored = json.loads((storage_root / locator).read_text())
    if "page_locators" in stored:
        return [json.loads((storage_root / loc).read_text())
                for loc in stored["page_locators"]]
    return stored["pages"]


def capability_names(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM capabilities ORDER BY id")]


def build_corpus(seed: int, per_stratum: int = 80,
                 per_snapshot: int = 8) -> list[dict]:
    """Deterministic sample of requirement-like sentences across every
    (ats_type, origin) stratum present in the live registry."""
    conn = read_only_connection()
    storage_root = instance_dir()
    adapters = build_adapters()
    rows = conn.execute(
        "SELECT s.ats_type, s.origin, s.tenant_slug, n.raw_locator"
        " FROM snapshots n JOIN sources s ON s.id = n.source_id"
        " WHERE n.posting_count > 0 ORDER BY n.id").fetchall()
    strata: dict[tuple, list] = {}
    for ats_type, origin, slug, locator in rows:
        strata.setdefault((ats_type, origin), []).append((slug, locator))

    corpus: list[dict] = []
    for stratum in sorted(strata):
        ats_type, origin = stratum
        rng = random.Random(f"{seed}:{ats_type}:{origin}")
        candidates = sorted(strata[stratum], key=lambda r: r[1])
        rng.shuffle(candidates)
        taken = 0
        # One sentence per posting and a per-snapshot cap, so a single large
        # tenant cannot supply a stratum's whole share of phrasing.
        for slug, locator in candidates:
            if taken >= per_stratum:
                break
            try:
                pages = _read_pages(storage_root, locator)
                jobs = adapters[ats_type].jobs_from_pages(pages)
            except (OSError, ValueError, KeyError):
                continue  # raw payloads are prunable operational data
            rng.shuffle(jobs)
            from_snapshot = 0
            for job in jobs:
                if taken >= per_stratum or from_snapshot >= per_snapshot:
                    break
                try:
                    normalized = adapters[ats_type].normalize(job)
                except (ValueError, KeyError, TypeError):
                    continue
                found = _sentences(normalized.get("description") or "")
                if not found:
                    continue
                corpus.append({
                    "ats_type": ats_type, "origin": origin,
                    "tenant": slug, "title": normalized.get("title"),
                    "phrase": found[rng.randrange(len(found))],
                })
                taken += 1
                from_snapshot += 1
    conn.close()
    return corpus


# --------------------------------------------------------------- the rules

def rule_all_tokens(phrase_tokens, cap_tokens, _content, _df, _n) -> bool:
    """The shipped baseline: every capability-name token in the phrase."""
    return bool(cap_tokens) and cap_tokens <= phrase_tokens


def rule_all_content(phrase_tokens, _cap, content, _df, _n) -> bool:
    """Every content token (stopwords and connectives dropped)."""
    return bool(content) and content <= phrase_tokens


def _fraction_rule(threshold_bp: int):
    def rule(phrase_tokens, _cap, content, _df, _n) -> bool:
        if not content:
            return False
        hit = len(content & phrase_tokens)
        return (10000 * hit) // len(content) >= threshold_bp
    rule.__name__ = f"content_fraction_{threshold_bp}bp"
    return rule


def _distinctive_rule(df_share_bp: int, minimum: int):
    """A token present in a large share of the corpus carries little signal;
    a match must land `minimum` distinctive content tokens."""
    def rule(phrase_tokens, _cap, content, df, n) -> bool:
        distinctive = {t for t in content
                       if (10000 * df.get(t, 0)) // max(n, 1) < df_share_bp}
        if not distinctive:
            return False
        return len(distinctive & phrase_tokens) >= minimum
    rule.__name__ = f"distinctive_df{df_share_bp}bp_min{minimum}"
    return rule


def _distinctive_fraction_rule(df_share_bp: int, threshold_bp: int):
    """Fraction rule computed over distinctive content tokens only."""
    def rule(phrase_tokens, _cap, content, df, n) -> bool:
        distinctive = {t for t in content
                       if (10000 * df.get(t, 0)) // max(n, 1) < df_share_bp}
        if not distinctive:
            return False
        hit = len(distinctive & phrase_tokens)
        return (10000 * hit) // len(distinctive) >= threshold_bp
    rule.__name__ = f"distinctive_df{df_share_bp}bp_fraction{threshold_bp}bp"
    return rule


def _combined_rule(df_share_bp: int, threshold_bp: int):
    """Both tests at once: the phrase must carry enough of the capability's
    content tokens AND at least one of its distinctive ones, so agreement on
    generic vocabulary alone (experience, systems, product) never matches."""
    def rule(phrase_tokens, _cap, content, df, n) -> bool:
        if not content:
            return False
        hit = content & phrase_tokens
        if (10000 * len(hit)) // len(content) < threshold_bp:
            return False
        return any((10000 * df.get(t, 0)) // max(n, 1) < df_share_bp
                   for t in hit)
    rule.__name__ = f"combined_df{df_share_bp}bp_fraction{threshold_bp}bp"
    return rule


def _idf_rule(threshold_bp: int):
    """Fraction weighted by corpus rarity: a token in most postings counts
    for little, a rare one for much."""
    def rule(phrase_tokens, _cap, content, df, n) -> bool:
        if not content:
            return False
        def weight(token):
            return math.log(max(n, 2) / (1 + df.get(token, 0)))
        total = sum(weight(t) for t in content)
        if total <= 0:
            return False
        hit = sum(weight(t) for t in content & phrase_tokens)
        return (10000 * hit) / total >= threshold_bp
    rule.__name__ = f"idf_fraction_{threshold_bp}bp"
    return rule


def _fraction_min_hits_rule(threshold_bp: int, min_hits: int):
    """The fraction rule with an absolute floor: a two-token capability may
    not match on one generic token alone."""
    def rule(phrase_tokens, _cap, content, _df, _n) -> bool:
        if not content:
            return False
        hit = content & phrase_tokens
        if len(hit) < min(min_hits, len(content)):
            return False
        return (10000 * len(hit)) // len(content) >= threshold_bp
    rule.__name__ = f"fraction{threshold_bp}bp_min{min_hits}hits"
    return rule


def candidate_rules() -> list:
    rules = [rule_all_tokens, rule_all_content]
    rules += [_fraction_rule(bp) for bp in (3300, 5000, 6600, 7500)]
    rules += [_distinctive_rule(bp, m)
              for bp in (500, 1000, 2000) for m in (1, 2)]
    rules += [_distinctive_fraction_rule(bp, f)
              for bp in (1000, 2000) for f in (5000, 6600)]
    rules += [_combined_rule(bp, f)
              for bp in (200, 500, 1000)
              for f in (3300, 4000, 5000, 6600)]
    rules += [_idf_rule(bp) for bp in (4000, 5000, 6000, 7000)]
    rules += [_fraction_min_hits_rule(f, m)
              for f in (3300, 4000, 5000, 6600) for m in (2, 3)]
    return rules


def _document_frequency(corpus) -> tuple[dict, int]:
    df: dict[str, int] = {}
    for entry in corpus:
        for token in normalized_tokens(entry["phrase"]):
            df[token] = df.get(token, 0) + 1
    return df, len(corpus)


def evaluate(corpus, capabilities, labels) -> dict:
    df, n = _document_frequency(corpus)
    caps = [(name, normalized_tokens(name), stopword_free_tokens(name))
            for name in capabilities]
    phrase_cache = {e["phrase"]: normalized_tokens(e["phrase"]) for e in corpus}
    for phrase, _cap, _truth in labels:
        phrase_cache.setdefault(phrase, normalized_tokens(phrase))

    report = {"corpus_size": n, "capabilities": len(capabilities),
              "labels": len(labels), "rules": []}
    for rule in candidate_rules():
        covered = 0
        pair_hits = 0
        for entry in corpus:
            tokens = phrase_cache[entry["phrase"]]
            hits = sum(1 for _n, cap, content in caps
                       if rule(tokens, cap, content, df, n))
            pair_hits += hits
            covered += 1 if hits else 0
        tp = fp = fn = tn = 0
        for (phrase, cap_name, truth) in labels:
            tokens = phrase_cache[phrase]
            cap = normalized_tokens(cap_name)
            content = stopword_free_tokens(cap_name)
            predicted = rule(tokens, cap, content, df, n)
            if predicted and truth:
                tp += 1
            elif predicted and not truth:
                fp += 1
            elif truth:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision and recall else 0.0)
        report["rules"].append({
            "rule": rule.__name__,
            "corpus_coverage_pct": round(100 * covered / max(n, 1), 2),
            "pairs_matched_pct": round(
                100 * pair_hits / max(n * len(caps), 1), 3),
            "labelled_tp": tp, "labelled_fp": fp,
            "labelled_fn": fn, "labelled_tn": tn,
            "precision": None if precision is None else round(precision, 3),
            "recall": None if recall is None else round(recall, 3),
            "f1": round(f1, 3),
            "accuracy": round((tp + tn) / max(len(labels), 1), 3),
        })
    return report


def load_labels() -> list[tuple[str, str, bool]]:
    if not LABELS_PATH.exists():
        return []
    data = json.loads(LABELS_PATH.read_text())
    return [(item["phrase"], item["capability"], bool(item["match"]))
            for item in data["labels"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=("corpus", "pairs", "evaluate", "shipped"))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--per-stratum", type=int, default=80)
    parser.add_argument("--pairs", type=int, default=80)
    args = parser.parse_args()

    corpus = build_corpus(args.seed, per_stratum=args.per_stratum)
    conn = read_only_connection()
    capabilities = capability_names(conn)
    conn.close()

    if args.command == "corpus":
        print(json.dumps({"size": len(corpus), "entries": corpus},
                         ensure_ascii=False, indent=1))
        return 0
    if args.command == "pairs":
        # A labelling worksheet: pairs any candidate rule would match, plus
        # a deterministic random tail, so the labels cover both sides.
        df, n = _document_frequency(corpus)
        rules = candidate_rules()
        flagged, others = [], []
        for entry in corpus:
            tokens = normalized_tokens(entry["phrase"])
            for name in capabilities:
                cap, content = (normalized_tokens(name),
                                stopword_free_tokens(name))
                pair = {"phrase": entry["phrase"], "capability": name}
                if any(r(tokens, cap, content, df, n) for r in rules):
                    flagged.append(pair)
                else:
                    others.append(pair)
        rng = random.Random(f"pairs:{args.seed}")
        rng.shuffle(flagged)
        rng.shuffle(others)
        half = args.pairs // 2
        print(json.dumps(flagged[:half] + others[:args.pairs - half],
                         ensure_ascii=False, indent=1))
        return 0
    if args.command == "shipped":
        # What the rule as shipped in domain/requirements.py does on this
        # corpus, at the configured threshold.
        phrases = tuple(e["phrase"] for e in corpus)
        matched = matched_requirements(phrases, capabilities)
        print(json.dumps({"corpus_size": len(corpus),
                          "threshold_bp": CONTENT_FRACTION_BP,
                          "matched": len(matched),
                          "coverage_pct": round(
                              100 * len(matched) / max(len(corpus), 1), 2)},
                         indent=1))
        return 0

    print(json.dumps(evaluate(corpus, capabilities, load_labels()), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
