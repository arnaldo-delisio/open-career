"""The canonical date parser (domain/dates.py): the single authority behind
every date comparison and ordering. The regression it exists for is the drive
finding that a normal four-role CV could never clear stage zero, because
"September 2015" > "July 2017" alphabetically."""

import pytest

from domain.dates import (
    CanonicalDate,
    chron_rank,
    is_open_ended,
    is_readable,
    parse,
    starts_before_end,
)

# The driver's real CV, verbatim.
DRIVER_ROLES = (
    ("September 2015", "July 2017"),
    ("August 2017", "May 2019"),
    ("June 2019", "February 2022"),
    ("March 2022", None),
)


@pytest.mark.parametrize("raw,expected", [
    ("2015-09", CanonicalDate(2015, 9)),
    ("2015-9", CanonicalDate(2015, 9)),
    ("2015-09-30", CanonicalDate(2015, 9)),
    ("2015", CanonicalDate(2015)),
    ("September 2015", CanonicalDate(2015, 9)),
    ("september 2015", CanonicalDate(2015, 9)),
    ("Sept. 2015", CanonicalDate(2015, 9)),
    ("Sep 2015", CanonicalDate(2015, 9)),
    ("  July   2017 ", CanonicalDate(2017, 7)),
    ("09/2015", CanonicalDate(2015, 9)),
    ("9/2015", CanonicalDate(2015, 9)),
    ("2015/09", CanonicalDate(2015, 9)),
])
def test_the_parser_reads_what_cvs_actually_write(raw, expected):
    assert parse(raw) == expected
    assert parse(raw).iso == expected.iso


@pytest.mark.parametrize("raw", ["03/04/2015", "mid-2015", "summer 2015",
                                 "Septembre 2015", "2015-13", "next year", "15-09"])
def test_ambiguous_or_unreadable_input_is_never_guessed_at(raw):
    assert parse(raw) is None
    assert is_readable(raw) is False


@pytest.mark.parametrize("raw", [None, "", "Present", "present", "current",
                                 "Ongoing", "to date"])
def test_open_ended_labels_are_recognized_not_guessed(raw):
    assert is_open_ended(raw)
    assert is_readable(raw)
    assert parse(raw) is None


def test_the_drivers_dates_all_cohere():
    """Each of the driver's four roles: start precedes end. Two of them
    (September->July, June->February) invert alphabetically, which is exactly
    what used to be reported as 'start follows end'."""
    for start, end in DRIVER_ROLES:
        assert starts_before_end(start, end) is not False


def test_the_drivers_roles_order_reverse_chronologically_open_ended_first():
    shuffled = [DRIVER_ROLES[0], DRIVER_ROLES[3], DRIVER_ROLES[2], DRIVER_ROLES[1]]
    ordered = sorted(shuffled, key=lambda r: chron_rank(*r), reverse=True)
    assert ordered == [
        ("March 2022", None),          # the current role leads
        ("June 2019", "February 2022"),
        ("August 2017", "May 2019"),
        ("September 2015", "July 2017"),
    ]


def test_a_real_inversion_is_still_caught():
    assert starts_before_end("July 2017", "September 2015") is False
    assert starts_before_end("2024-05", "2022-03") is False


def test_comparison_runs_at_the_coarsest_shared_precision():
    """A year-only label states no month, so it can contradict nothing at
    month precision. Padding it to January would manufacture a failure."""
    assert starts_before_end("2015", "2015-09") is not False
    assert starts_before_end("2015-09", "2015") is not False
    assert starts_before_end("2016", "2015-09") is False


def test_an_undecidable_comparison_is_none_never_a_contradiction():
    assert starts_before_end("mid-2015", "2017-07") is None
    assert starts_before_end("2015-09", None) is None


def test_unreadable_dates_rank_oldest_and_never_lead_the_section():
    ranks = [chron_rank("mid-2015", "whenever"), chron_rank("March 2022", None)]
    assert sorted(ranks, reverse=True)[0] == chron_rank("March 2022", None)
