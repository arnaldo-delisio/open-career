"""Canonical experience dates: one parser, used by every comparison and every
ordering in the system.

The stored `start_date`/`end_date` strings are DISPLAY labels, kept verbatim as
the user wrote them (extraction's prompt says "as written in the CV", the
grounding verifier compares the rendered entry against the canonical row
character for character, and the renderer prints exactly those characters).
They are deliberately NOT the canonical time value: "September 2015" and
"2015-09" are the same month written two ways, and no lexical comparison of
those labels means anything.

The canonical time value is the (year, month) pair this module derives from a
label. Every place that asks "is this before that" or "which order do these go
in" derives it here and nowhere else; comparing the raw label was the defect
that made a normal CV unjudgeable ("September 2015" > "July 2017"
alphabetically, so date-coherence failed with a confident falsehood and the
experience section sorted the current role last).

Three properties the parser holds to:

- **It accepts what people and CVs actually write**: ISO (`2015-09`,
  `2015-09-30`, `2015`), month names and abbreviations (`September 2015`,
  `Sept. 2015`), and numeric month/year (`09/2015`, `9-2015`).
- **It never guesses.** An input it cannot read unambiguously (`03/04/2015`,
  `mid-2015`) parses to None, and the callers report the date as unreadable
  rather than inventing a month. Silently reinterpreting a date is worse than
  declining to judge it.
- **Precision is respected.** A year-only label carries no month, so a
  comparison against it happens at year precision; padding it to January would
  manufacture a contradiction that the data does not state.

Open-endedness (a current role) is null `end_date`. The words a human types
for it (`Present`, `Current`, `Ongoing`, ...) are recognized here so a write
path can offer to store the null, and so a label that slipped in anyway orders
and compares as open-ended rather than as unreadable.
"""

import re
from dataclasses import dataclass

# Sort ranks. An open-ended (current) role is the most recent thing there is;
# an unreadable label ranks oldest and is reported, never quietly ordered.
OPEN_ENDED_RANK = (9999, 99)
UNREADABLE_RANK = (0, 0)

MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# The vocabulary for "this role has not ended". Recognized, not guessed at:
# each of these words means open-ended and nothing else.
OPEN_ENDED_WORDS = frozenset({
    "present", "current", "currently", "now", "ongoing", "to date", "today",
    "in progress",
})

_SEP = r"[\s./-]+"
_ISO = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-\d{1,2})?$")
_YEAR_ONLY = re.compile(r"^(\d{4})$")
_NAME_YEAR = re.compile(rf"^([a-z]+)\.?{_SEP}(\d{{4}})$")
_YEAR_NAME = re.compile(rf"^(\d{{4}}){_SEP}([a-z]+)\.?$")
_NUM_YEAR = re.compile(rf"^(\d{{1,2}}){_SEP}(\d{{4}})$")
_YEAR_NUM = re.compile(rf"^(\d{{4}}){_SEP}(\d{{1,2}})$")


@dataclass(frozen=True)
class CanonicalDate:
    """A month, or a year when the label named no month."""

    year: int
    month: int | None = None

    @property
    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}" if self.month else f"{self.year:04d}"


def _clean(raw: str | None) -> str:
    return " ".join(str(raw or "").strip().lower().split())


def is_open_ended(raw: str | None) -> bool:
    """A null, blank, or explicitly open-ended label: the role has not ended."""
    cleaned = _clean(raw)
    return not cleaned or cleaned.strip(".,") in OPEN_ENDED_WORDS


def parse(raw: str | None) -> CanonicalDate | None:
    """The canonical time value behind a stored label, or None when the label
    is open-ended or cannot be read unambiguously."""
    cleaned = _clean(raw)
    if not cleaned or is_open_ended(cleaned):
        return None
    if _YEAR_ONLY.match(cleaned):
        return CanonicalDate(int(cleaned))
    iso = _ISO.match(cleaned)
    if iso:
        return _month(int(iso.group(1)), int(iso.group(2)) if iso.group(2) else None)
    for pattern, year_first in ((_NAME_YEAR, False), (_YEAR_NAME, True)):
        match = pattern.match(cleaned)
        if match:
            year, name = ((match.group(1), match.group(2)) if year_first
                          else (match.group(2), match.group(1)))
            if name in MONTH_NAMES:
                return CanonicalDate(int(year), MONTH_NAMES[name])
            return None
    for pattern, year_first in ((_NUM_YEAR, False), (_YEAR_NUM, True)):
        match = pattern.match(cleaned)
        if match:
            year, month = ((match.group(1), match.group(2)) if year_first
                           else (match.group(2), match.group(1)))
            return _month(int(year), int(month))
    return None


def _month(year: int, month: int | None) -> CanonicalDate | None:
    if month is not None and not 1 <= month <= 12:
        return None
    return CanonicalDate(year, month)


def is_readable(raw: str | None) -> bool:
    """Can this label be judged at all? Open-ended counts: it states something
    definite. Only a label the parser cannot read is unreadable."""
    return is_open_ended(raw) or parse(raw) is not None


def start_rank(raw: str | None) -> tuple[int, int]:
    """Sort rank of a start label. A missing month ranks at the start of the
    year, which is where an unstated month can only fall."""
    parsed = parse(raw)
    if parsed is None:
        return UNREADABLE_RANK
    return (parsed.year, parsed.month or 1)


def end_rank(raw: str | None) -> tuple[int, int]:
    """Sort rank of an end label: open-ended ranks above every dated end, a
    missing month at the end of the year."""
    if is_open_ended(raw):
        return OPEN_ENDED_RANK
    parsed = parse(raw)
    if parsed is None:
        return UNREADABLE_RANK
    return (parsed.year, parsed.month or 12)


def chron_rank(start: str | None, end: str | None) -> tuple[tuple[int, int], tuple[int, int]]:
    """The reverse-chronological sort key: by end (open-ended first), then by
    start. Sorted with reverse=True this is the order a CV reads in."""
    return (end_rank(end), start_rank(start))


def starts_before_end(start: str | None, end: str | None) -> bool | None:
    """Does the start precede the end? None when the question does not arise
    (an open-ended or absent side) or cannot be answered (an unreadable
    label): a caller reports that, it never counts as a contradiction.

    The comparison runs at the coarsest precision the two labels share, so
    "2015" to "2015-09" is not a contradiction: the year-only label states no
    month to contradict with."""
    if is_open_ended(start) or is_open_ended(end):
        return None
    first, last = parse(start), parse(end)
    if first is None or last is None:
        return None
    if first.month is None or last.month is None:
        return first.year <= last.year
    return (first.year, first.month) <= (last.year, last.month)
