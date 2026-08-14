"""The two discovery model stages (OC-37 §5): minimal requirement extraction
and the one judged fit with a written reason, both through ModelAdapter under
the run budget, both inside the untrusted-content isolation boundary.

The posting is data, never instructions: it travels JSON-encoded inside a
fenced block beneath a stated boundary (the pattern cv_generation.md set), and
output is validated against a closed schema in code with one retry (OC-5: the
model proposes structure, code decides). Coverage against the target-family
vocabulary is computed here deterministically, zero model involvement.
"""

import json
import re
from dataclasses import dataclass

from domain.ports import ModelAdapter

MAX_REQUIREMENTS = 25
MAX_REQUIREMENT_WORDS = 12
MAX_REQUIREMENT_CHARS = 200
FIT_BANDS = ("low", "medium", "high")

# OC-13 deterministic backstop, code not prompt trust: posting-derived
# requirement phrases carrying authenticity-verdict language are
# schema-invalid at extraction parse time and never persist; the row ends
# terminal failed rather than rendered (a rare false positive is acceptable;
# a rendered verdict is not). Matching is token-stem based, case-insensitive.
# Rejection messages stay neutral and never reproduce the rejected text. The
# judged-fit stage has no free text at all: its reason is rendered in code
# from stored phrases as explicitly attributed posting quotes.
FORBIDDEN_TERM_STEMS: tuple[str, ...] = (
    "ghost", "fake", "fraud", "scam", "legitim", "authentic", "bogus", "sham",
)


class StageOutputError(ValueError):
    """Model output failed the closed-schema validation."""


class BudgetExhausted(RuntimeError):
    """The budget refused the next model call (retry included); the caller
    stops the stage cleanly and the row stays claimable for the next run."""


def _charged(charge) -> None:
    """Every ModelAdapter.complete invocation is charged BEFORE it is made;
    a refused charge stops the stage, it never lets the call slip through."""
    if charge is not None and not charge():
        raise BudgetExhausted("model-call budget refused the next call")


def _single_line_ascii_json(obj) -> str:
    """JSON encoding with the fence guarantee: ensure_ascii turns every
    non-ASCII character (Unicode line separators U+2028/U+2029 included) into
    an escape, so the payload is one physical ASCII line and a fence delimiter
    can never appear at line start inside the data block. The invariant is
    asserted in code, never assumed."""
    encoded = json.dumps(obj, ensure_ascii=True, sort_keys=True)
    if any(ch in encoded for ch in ("\n", "\r", "\u2028", "\u2029")):
        raise ValueError("payload encoding violated the single-line invariant")
    return encoded


def build_posting_json(title: str | None, description: str | None,
                       locations: list | None = None,
                       salary: dict | None = None) -> str:
    """The isolation payload: the posting's fields JSON-encoded, so posting
    text can never terminate the data block or masquerade as prompt text."""
    return _single_line_ascii_json({
        "title": title,
        "description": description,
        "locations": list(locations or []),
        "salary": salary,
    })


_PLACEHOLDER = re.compile(r"\{(posting_json|requirements_json|candidate_json)\}")


def _render(template: str, values: dict[str, str]) -> str:
    """Single-pass substitution: each placeholder in the TEMPLATE is replaced
    once and no replacement text is ever rescanned, so placeholder tokens
    arriving inside posting data stay literal data (§5 boundary). Sequential
    str.replace would rescan earlier substitutions and splice values into the
    posting payload."""
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def render_extraction_prompt(template: str, posting_json: str) -> str:
    return _render(template, {"posting_json": posting_json})


def render_judgment_prompt(template: str, posting_json: str,
                           requirements: tuple, candidate: dict) -> str:
    # Requirements carry posting-derived text, so they get the same
    # single-line ASCII fence guarantee as the posting payload.
    return _render(template, {
        "posting_json": posting_json,
        "requirements_json": _single_line_ascii_json(list(requirements)),
        "candidate_json": _single_line_ascii_json(candidate),
    })


def _parse_json_object(text: str, expected_keys: set[str]) -> dict:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise StageOutputError(f"output is not valid JSON: {e}") from e
    if not isinstance(obj, dict) or set(obj) != expected_keys:
        raise StageOutputError(
            f"top level must be an object with exactly {sorted(expected_keys)}")
    return obj


def _normalize_for_match(text: str) -> str:
    """Case-and-whitespace normalization for the excerpt provenance check."""
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _posting_haystack(posting_json: str) -> str:
    """The normalized posting text an extracted phrase must be an excerpt of:
    title, description, and location strings from the isolation payload."""
    payload = json.loads(posting_json)
    parts = [payload.get("title") or "", payload.get("description") or ""]
    parts.extend(str(loc) for loc in payload.get("locations") or [])
    return _normalize_for_match(" ".join(parts))


def parse_requirements(text: str,
                       posting_json: str | None = None) -> tuple[str, ...]:
    """Closed schema: {"requirements": [phrase, ...]}, each a non-empty string
    of at most MAX_REQUIREMENT_WORDS words and MAX_REQUIREMENT_CHARS chars, at
    most MAX_REQUIREMENTS, no duplicates.

    Structural provenance: when posting_json is given, every phrase must be a
    verbatim excerpt of the posting (case-and-whitespace-normalized substring
    check in code), so an invented phrase, verdict language included, is
    structurally impossible; the term denylist stays as a belt for verdicts
    the posting itself contains. Anything else raises, neutrally."""
    obj = _parse_json_object(text, {"requirements"})
    items = obj["requirements"]
    if not isinstance(items, list) or len(items) > MAX_REQUIREMENTS:
        raise StageOutputError(
            f"'requirements' must be an array of at most {MAX_REQUIREMENTS}")
    haystack = _posting_haystack(posting_json) if posting_json is not None else None
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise StageOutputError(f"requirements[{i}] must be a non-empty string")
        if len(item) > MAX_REQUIREMENT_CHARS:
            raise StageOutputError(
                f"requirements[{i}] exceeds {MAX_REQUIREMENT_CHARS} characters")
        if len(item.split()) > MAX_REQUIREMENT_WORDS:
            raise StageOutputError(
                f"requirements[{i}] exceeds {MAX_REQUIREMENT_WORDS} words")
        if item.strip() in out:
            raise StageOutputError(f"requirements[{i}] is a duplicate")
        tokens = _TOKEN.findall(item.lower())
        if any(token.startswith(stem) for token in tokens
               for stem in FORBIDDEN_TERM_STEMS):
            # Neutral by design: the rejected text never travels in the error.
            raise StageOutputError(
                f"requirements[{i}] failed validation: authenticity-verdict"
                " language (OC-13)")
        if haystack is not None and _normalize_for_match(item) not in haystack:
            raise StageOutputError(
                f"requirements[{i}] failed validation: not a verbatim excerpt"
                " of the posting text")
        out.append(item.strip())
    return tuple(out)


@dataclass(frozen=True)
class JudgedFit:
    """Validated structure, no free text: the model selects a fit band and
    partitions the extracted requirement ids into matches and gaps; the
    user-facing reason is rendered in code from the ids' stored phrases."""

    fit: str
    matched_requirement_ids: tuple[str, ...]
    gap_requirement_ids: tuple[str, ...]


def parse_judged_fit(text: str, known_ids: tuple) -> JudgedFit:
    """Closed schema: {"fit": low|medium|high, "matched_requirement_ids":
    [...], "gap_requirement_ids": [...]}. Every id must be an extracted
    requirement id for this row; unknown ids, duplicates, or an id appearing
    on both sides are schema-invalid. No free-text field exists."""
    obj = _parse_json_object(
        text, {"fit", "matched_requirement_ids", "gap_requirement_ids"})
    if obj["fit"] not in FIT_BANDS:
        raise StageOutputError(f"'fit' must be one of {list(FIT_BANDS)}")
    known = set(known_ids)
    sides: dict[str, tuple[str, ...]] = {}
    for key in ("matched_requirement_ids", "gap_requirement_ids"):
        ids = obj[key]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise StageOutputError(f"'{key}' must be an array of strings")
        if len(set(ids)) != len(ids):
            raise StageOutputError(f"'{key}' contains duplicates")
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise StageOutputError(
                f"'{key}' names ids that are not extracted requirement ids"
                " for this row")
        sides[key] = tuple(ids)
    overlap = set(sides["matched_requirement_ids"]) \
        & set(sides["gap_requirement_ids"])
    if overlap:
        raise StageOutputError(
            "an id appears as both a match and a gap")
    return JudgedFit(fit=obj["fit"],
                     matched_requirement_ids=sides["matched_requirement_ids"],
                     gap_requirement_ids=sides["gap_requirement_ids"])


def render_judged_reason(judged: JudgedFit, phrases_by_id: dict) -> str:
    """The deterministic presentation backstop (work-loop rule 5): the
    rendered reason contains only stored requirement phrases plus fixed
    template words, assembled in code, never model prose. Every phrase is an
    explicitly attributed posting quote, so any language inside it is visibly
    the posting's own words, never this tool's assertion (OC-13)."""
    matches = [f'"{phrases_by_id[i]}"' for i in judged.matched_requirement_ids]
    gaps = [f'"{phrases_by_id[i]}"' for i in judged.gap_requirement_ids]
    parts = []
    if matches:
        parts.append("matches posting text: " + ", ".join(matches))
    if gaps:
        parts.append("gaps vs posting text: " + ", ".join(gaps))
    return "; ".join(parts) if parts else "no requirement signal cited"


class RequirementExtractionService:
    """One model call, closed-schema validation, one retry carrying the
    validation error back (the extraction.py pattern)."""

    def __init__(self, model: ModelAdapter, template: str):
        self._model = model
        self._template = template

    def extract(self, posting_json: str, charge=None) -> tuple[str, ...]:
        prompt = render_extraction_prompt(self._template, posting_json)
        _charged(charge)
        try:
            return parse_requirements(self._model.complete(prompt), posting_json)
        except StageOutputError as e:
            retry = (f"{prompt}\n\nYour previous output failed validation:"
                     f" {e}. Return ONLY the JSON object, exactly the schema.")
            _charged(charge)  # the schema retry is a model call too
            return parse_requirements(self._model.complete(retry), posting_json)


class JudgedFitService:
    def __init__(self, model: ModelAdapter, template: str):
        self._model = model
        self._template = template

    def judge(self, posting_json: str, requirements: tuple,
              candidate: dict, charge=None) -> JudgedFit:
        """requirements is a tuple of {"id", "phrase"} dicts; the model may
        only select among their ids (validated in code)."""
        known_ids = tuple(r["id"] for r in requirements)
        prompt = render_judgment_prompt(self._template, posting_json,
                                        requirements, candidate)
        _charged(charge)
        try:
            return parse_judged_fit(self._model.complete(prompt), known_ids)
        except StageOutputError as e:
            retry = (f"{prompt}\n\nYour previous output failed validation:"
                     f" {e}. Return ONLY the JSON object, exactly the schema.")
            _charged(charge)  # the schema retry is a model call too
            return parse_judged_fit(self._model.complete(retry), known_ids)


# --- deterministic coverage (§5): normalized token match, zero model ----------

_TOKEN = re.compile(r"[a-z0-9]+")

# Connectives and structural words carry no matching signal: a vocabulary term
# like "Governance and human-in-the-loop controls" is about governance,
# human, loop and controls, and requiring "and", "in" and "the" to appear
# verbatim in one excerpt is what made the shipped all-tokens rule match
# nothing at all. A closed, tested constant, never a model.
VOCABULARY_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on",
    "or", "the", "to", "with", "within", "across", "over", "under", "via",
    "end", "e2e", "using", "based",
})

# The fraction of a vocabulary term's content tokens a phrase must carry, in basis
# points, calibrated in scripts/calibrate_evidence_matcher.py against real
# postings and a hand-labelled sample. Configurable because the threshold is a
# judgment call; the calibration numbers are recorded in the design doc §5.
CONTENT_FRACTION_BP = 5000

# Below this many content tokens, a term must match in FULL rather than at the
# fraction above. The threshold was calibrated on capability names, which are
# long and specific; since OC-42 the vocabulary also carries family names and
# adjacent titles ("Client Specialist", "Product Manager"), and those are short
# labels built almost entirely of generic words. At 5000bp a two-token title
# matches on either half alone, so "Client Specialist" scores a hit on any
# requirement containing "specialist" and coverage inflates on ordinary words.
# Requiring every content token of a short term keeps them honest, and leaves
# the calibrated fraction untouched for the 3+ token terms it was measured on.
SHORT_TERM_MAX_CONTENT_TOKENS = 2


def normalized_tokens(text: str) -> frozenset[str]:
    """Lowercased alphanumeric tokens: the shared normalization both sides of
    the match run through."""
    return frozenset(_TOKEN.findall(text.lower()))


def _ordered_content_tokens(text: str) -> tuple[str, ...]:
    """The same normalization as stopword_free_tokens, with token ORDER kept
    and repeats dropped. Used only as a dedup signature
    (title_relevance_score): as a set, "Data Science" and "Science Data"
    collapse into one term and one of them loses its point, and they are
    genuinely different terms. Repeats go because the matcher itself works on a
    frozenset, so "Data" and "Data Data" are matching-equivalent; counting them
    as two terms would let a repeated-token spelling raise a posting's
    relevance, and its paid-stage ordering, on no extra title evidence."""
    return tuple(dict.fromkeys(token for token in _TOKEN.findall(text.lower())
                               if token not in VOCABULARY_STOPWORDS))


def stopword_free_tokens(text: str) -> frozenset[str]:
    """A vocabulary term's content tokens: normalized, connectives dropped.
    A term made only of connectives ("end-to-end") has no content and matches
    nothing: keeping its tokens would turn the emptiest possible term into
    the broadest match key, inflating coverage on ordinary words (Codex r3)."""
    return normalized_tokens(text) - VOCABULARY_STOPWORDS


def vocabulary_matches(requirement: str, vocabulary: list,
                       threshold_bp: int = CONTENT_FRACTION_BP) -> list[str]:
    """The target-family vocabulary terms matching one requirement phrase: at
    least `threshold_bp` of the term's content tokens present in the phrase's
    normalized tokens, except short terms (SHORT_TERM_MAX_CONTENT_TOKENS or
    fewer content tokens), which must match in full. Deterministic, zero model
    involvement (OC-37 §5)."""
    phrase_tokens = normalized_tokens(requirement)
    matched = []
    for name in vocabulary:
        content = stopword_free_tokens(name)
        if not content:
            continue
        hit = len(content & phrase_tokens)
        if len(content) <= SHORT_TERM_MAX_CONTENT_TOKENS:
            if hit == len(content):
                matched.append(name)
        elif (10000 * hit) // len(content) >= threshold_bp:
            matched.append(name)
    return matched


def title_relevance_score(title: str | None, vocabulary: list,
                          threshold_bp: int = CONTENT_FRACTION_BP) -> int:
    """How many distinct target-family vocabulary terms a posting's TITLE
    matches. Zero means the title carries no target-family signal at all.

    The same matcher coverage uses, applied to the one material field that is
    present before any model call, so it can decide which rows are worth one
    (workers/discovery/run.py). Deterministic, zero model involvement, and the
    short-term full-match rule still applies, which is what stops a two-token
    family name scoring a hit on any title containing 'specialist'.

    It lives here rather than in promotion.py because it is a vocabulary-match
    operation and reusing vocabulary_matches in place is the whole point; a
    second matcher would be a second thing to calibrate. promotion.py takes the
    resulting integer, not the vocabulary."""
    if not title:
        return 0
    # Counted by normalized content-token signature, not by raw string: two
    # spellings of one term ("Data Engineer", "data-engineer") are the same
    # term to this matcher, and letting each score a point would make the
    # numbers incomparable across vocabulary edits that only add a spelling.
    # The signature keeps token ORDER, so a permutation ("Data Science" and
    # "Science Data") stays two terms, which is what it is.
    #
    # Nested terms are deliberately NOT collapsed: a title matching both
    # "Engineer" and "Data Engineer" carries more target-family signal than
    # one matching "Engineer" alone, and this score exists to rank exactly
    # that. Please do not "fix" it into an overlap-group count.
    return len({_ordered_content_tokens(name) for name
                in vocabulary_matches(title, vocabulary, threshold_bp)})


def matched_requirements(requirements: tuple, vocabulary: list,
                         threshold_bp: int = CONTENT_FRACTION_BP) -> list[str]:
    """Requirements matched by at least one vocabulary term (normalized token
    match on content tokens: terms of SHORT_TERM_MAX_CONTENT_TOKENS or fewer
    content tokens must match in full, and only longer terms use the
    calibrated fraction threshold)."""
    return [requirement for requirement in requirements
            if vocabulary_matches(requirement, vocabulary, threshold_bp)]


def coverage_bp(requirements: tuple, vocabulary: list,
                threshold_bp: int = CONTENT_FRACTION_BP) -> int:
    """Coverage as integer basis points: the fraction of extracted
    requirements matched by a target-family vocabulary term. No
    requirements = 0."""
    if not requirements:
        return 0
    matched = matched_requirements(requirements, vocabulary, threshold_bp)
    return (10000 * len(matched)) // len(requirements)
