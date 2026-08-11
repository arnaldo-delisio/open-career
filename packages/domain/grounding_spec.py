"""Versioned normalization and tokenization spec (spec:
decisions/package-generation-design.md, "The output gate").

Every text check in the grounding verifier and the pdftotext section check
runs over this one spec; SPEC_VERSION persists in the verifier report so a
stored package version is re-checkable later against the exact rules that
produced it. Changing anything here bumps SPEC_VERSION.

Covers: Unicode normalization (plus the mojibake incident classes: em-dashes,
smart quotes, zero-width characters, NBSP), case folding, a lemmatization
table, number and date canonicalization (word and digit forms, units kept
with values), and an explicit non-splitting rule for URLs, version strings,
and acronyms."""

import re
import unicodedata

SPEC_VERSION = "1"

# Pre-render and pre-compare character normalization (the mojibake-in-sent-
# letters incident class): em/en dashes, smart quotes, zero-width, NBSP.
_CHAR_MAP = {
    "—": "-", "–": "-", "−": "-",           # em/en dash, minus
    "‘": "'", "’": "'", "“": '"', "”": '"',
    " ": " ", " ": " ", " ": " ",           # NBSP variants
    "​": "", "‌": "", "‍": "", "﻿": "",  # zero-width
    "•": " ", "·": " ", "▪": " ", "⁃": " ",  # bullet glyphs
    "ﬁ": "fi", "ﬂ": "fl",                        # ligatures
}

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90", "hundred": "100", "thousand": "1000", "million": "1000000",
    "billion": "1000000000",
}

_MONTHS = {
    "january": "1", "february": "2", "march": "3", "april": "4", "may": "5",
    "june": "6", "july": "7", "august": "8", "september": "9", "october": "10",
    "november": "11", "december": "12",
    "jan": "1", "feb": "2", "mar": "3", "apr": "4", "jun": "6", "jul": "7",
    "aug": "8", "sep": "9", "sept": "9", "oct": "10", "nov": "11", "dec": "12",
}

# Closed function-word list: the model may connect grounded content words with
# these; nothing else enters ungrounded.
FUNCTION_WORDS = frozenset("""
a an the and or but nor so yet for of in on at by to from with within without
into onto over under between across through during before after until since
about against among around behind below beneath beside beyond down off out up
above near past toward towards as is are was were be been being am do does did
done doing have has had having will would shall should can could may might
must not no than then that this these those it its they them their theirs he
she his her hers we us our ours you your yours i me my mine who whom whose
which what when where why how there here all each both any some such own same
other another more most less least very too also just only while if because
although though whether per via using etc et al plus including include
includes included e.g i.e
""".split())

_WHITESPACE = re.compile(r"\s+")
# Non-splitting rule: URLs, emails, version strings, acronyms with dots,
# tech tokens (C++, C#, node.js, K8s) survive as single tokens.
_TOKEN = re.compile(
    r"https?://\S+|www\.\S+"                       # URLs
    r"|[\w.+-]+@[\w.-]+\.\w+"                      # emails
    r"|[$€£]\d+(?:[.,]\d+)*(?:%|[a-zA-Z]{1,2})?"   # currency amounts ($2M)
    r"|\d+(?:[.,]\d+)*%"                           # percentages
    r"|\d+(?:\.\d+)+"                              # version strings / decimals
    r"|(?:[A-Za-z]\.){2,}"                         # dotted acronyms (e.g. B.Sc.)
    r"|[A-Za-z0-9][\w+#.']*[\w+#]|[A-Za-z0-9]"     # words / tech tokens
)

# Numeric token with optional currency prefix and unit suffix; units are kept
# with values (a number with units must match value and unit).
_NUMERIC = re.compile(
    r"(?P<currency>[$€£])?"
    r"(?P<value>\d+(?:[.,]\d+)*)"
    r"(?P<unit>%|[kKmMbB](?![\w])|x(?![\w])|[a-zA-Z]{1,4}(?![\w]))?")

# Lemmatization: explicit irregulars first, then versioned suffix rules.
_IRREGULAR_LEMMAS = {
    "led": "lead", "leading": "lead", "leads": "lead",
    "built": "build", "building": "build", "builds": "build",
    "ran": "run", "running": "run", "runs": "run",
    "made": "make", "making": "make", "makes": "make",
    "grew": "grow", "grown": "grow", "growing": "grow", "grows": "grow",
    "held": "hold", "holding": "hold", "holds": "hold",
    "wrote": "write", "written": "write", "writing": "write", "writes": "write",
    "drove": "drive", "driven": "drive", "driving": "drive", "drives": "drive",
    "won": "win", "winning": "win", "wins": "win",
    "sold": "sell", "selling": "sell", "sells": "sell",
    "brought": "bring", "taught": "teach", "kept": "keep", "left": "leave",
    "founded": "found", "founding": "found", "founds": "found",
    "shipped": "ship", "shipping": "ship", "ships": "ship",
    "cut": "cut", "set": "set", "people": "person", "teams": "team",
}

_DOUBLED = re.compile(r"([bdfglmnprt])\1$")


def _suffix_lemma(word: str) -> str:
    for suffix, replacement in (("ies", "y"), ("ied", "y"), ("sses", "ss"),
                                ("ches", "ch"), ("shes", "sh"), ("xes", "x")):
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            return word[: -len(suffix)] + replacement
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            stem = word[: -len(suffix)]
            stem = _DOUBLED.sub(r"\1", stem)
            return stem
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


def lemma(token: str) -> str:
    token = _NUMBER_WORDS.get(token, token)
    token = _IRREGULAR_LEMMAS.get(token, token)
    return _suffix_lemma(token)


def normalize_chars(text: str) -> str:
    """Unicode NFKC plus the explicit character map; whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _CHAR_MAP.items():
        text = text.replace(src, dst)
    return _WHITESPACE.sub(" ", text).strip()


def normalize(text: str) -> str:
    return normalize_chars(text).casefold()


def tokenize(text: str) -> list[str]:
    """Tokens of the character-normalized, case-folded text."""
    return _TOKEN.findall(normalize(text))


def tokenize_preserving_case(text: str) -> list[str]:
    """Tokens with original casing kept (entity detection needs it)."""
    return _TOKEN.findall(normalize_chars(text))


def lemma_set(text: str) -> set[str]:
    return {lemma(t) for t in tokenize(text)}


def content_lemmas(text: str) -> set[str]:
    """Non-function-word lemmas, excluding pure numerics (rule 1 owns those)."""
    result = set()
    for token in tokenize(text):
        if token in FUNCTION_WORDS:
            continue
        base = lemma(token)
        if base in FUNCTION_WORDS or _is_pure_numeric(token):
            continue
        result.add(base)
    return result


def _is_pure_numeric(token: str) -> bool:
    return _NUMERIC.fullmatch(token) is not None or token in _NUMBER_WORDS


_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b")

_NUMERIC_SPAN = re.compile(
    r"(?P<currency>[$€£])?"
    r"(?P<value>\d+(?:[.,]\d+)*)"
    r"(?P<unit>\s?%|[a-zA-Z]{1,4}(?![\w]))?")  # % may be spaced; letter units attached


def numeric_tokens(text: str) -> set[tuple[str, str, str]]:
    """(currency, value, unit) triples for every numeric occurrence, after
    word-number and month canonicalization. Units are case-folded; values keep
    their digits with thousands separators stripped."""
    result = set()
    for token in tokenize(text):
        month = _MONTHS.get(token)
        if month:
            result.add(("", month, "month"))
    # Word numbers become digits on the normalized text itself, so a detached
    # unit ("forty %") stays adjacent to its value.
    canonical = _NUMBER_WORD_RE.sub(lambda m: _NUMBER_WORDS[m.group(0)], normalize(text))
    for m in _NUMERIC_SPAN.finditer(canonical):
        value = m.group("value").replace(",", "")
        unit = (m.group("unit") or "").strip().casefold()
        result.add((m.group("currency") or "", value, unit))
    return result


def is_entity_like(token: str) -> bool:
    """Capitalized, ALL-CAPS acronym, CamelCase, or letter-digit technical
    tokens: the classes the entity allowlist rule fails closed on."""
    if len(token) < 2 or not any(c.isalpha() for c in token):
        return False
    if token[0].isupper():
        return True
    return any(c.isupper() for c in token[1:]) or (
        any(c.isdigit() for c in token) and any(c.isalpha() for c in token))
