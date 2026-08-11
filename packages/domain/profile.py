"""User profile: the closed canonical field set (OC-29) and write validation.

28 profile-derivable fields, one JSON document in the single-row user_profile
table. Anything not answerable from the profile alone is a custom question for
the Resolution/authored-answer lane, never a schema field. The set is validated
here, in the domain, not frozen into DDL.
"""

import re
from urllib.parse import urlparse

# Grouped per the OC-29 fixture corpus: identity, contact, links, location,
# work authorization, logistics, consent, EEO/demographic block.
CANONICAL_PROFILE_FIELDS: frozenset[str] = frozenset({
    # identity
    "full_name", "preferred_name", "pronouns",
    # contact
    "email", "phone",
    # links
    "linkedin_url", "github_url", "portfolio_url", "website_url",
    # location
    "location", "country",
    # work authorization
    "authorized_in_country", "needs_sponsorship", "relocation", "remote_preference",
    # logistics
    "current_company", "current_title", "notice_period", "salary_expectation",
    # consent
    "privacy_consent", "future_contact_consent",
    # EEO / demographic (option mapping is templated per platform+region)
    "eeo_gender", "eeo_hispanic_latino", "eeo_race_ethnicity", "eeo_veteran",
    "eeo_disability", "eeo_orientation", "eeo_age_band",
})


class UnknownProfileFieldError(ValueError):
    pass


class InvalidProfileValueError(ValueError):
    pass


# Mechanical shape checks for the fields that later feed application forms
# directly; everything else stays free text.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9-]+$")
_URL_FIELDS = {"linkedin_url", "github_url", "portfolio_url", "website_url"}


def _looks_like_url(value: str) -> bool:
    """Optional http/https scheme (case-insensitive), a plausible dot-separated
    hostname, and any port/path/query/fragment."""
    candidate = value if "://" in value else f"https://{value}"
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https") or not host:
        return False
    labels = host.split(".")
    return len(labels) >= 2 and all(label and _HOST_LABEL.match(label) for label in labels)


def validate_profile_field(field: str) -> None:
    """The set is closed: an unknown field is an error, never a silent write."""
    if field not in CANONICAL_PROFILE_FIELDS:
        raise UnknownProfileFieldError(
            f"unknown profile field '{field}'; canonical fields: "
            + ", ".join(sorted(CANONICAL_PROFILE_FIELDS))
        )


def validate_profile_value(field: str, value: str | None) -> None:
    """Shape-check the obviously mechanical fields (email, link URLs). None
    (clearing a field) is always allowed."""
    if value is None:
        return
    if field == "email" and not _EMAIL_SHAPE.match(value):
        raise InvalidProfileValueError(
            f"'{value}' does not look like an email address (expected name@domain.tld)")
    if field in _URL_FIELDS and not _looks_like_url(value):
        raise InvalidProfileValueError(
            f"'{value}' does not look like a URL (expected e.g. https://example.com/...)")
