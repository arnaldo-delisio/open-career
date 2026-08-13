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

# Closed yes/no fields. These are not free text: the work-authorization
# projection (the Gauntlet's stage-zero policy input) is defined over exactly
# the strings "yes" and "no", so a "y" accepted here is a package that can
# never be judged. The obvious synonyms are accepted EXPLICITLY and mapped to
# the canonical value; anything else is refused at the seam with the closed
# set named, never stored and rejected six minutes later.
YES_NO_CHOICES: tuple[str, ...] = ("yes", "no")
YES_NO_FIELDS = frozenset({"authorized_in_country", "needs_sponsorship",
                           "relocation", "future_contact_consent"})
_YES_NO_SYNONYMS = {"y": "yes", "yes": "yes", "yeah": "yes", "true": "yes",
                    "n": "no", "no": "no", "nope": "no", "false": "no"}
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


def normalize_profile_value(field: str, value: str | None) -> str | None:
    """Canonical storage form: a scheme-less URL that passed validation is
    stored with https:// prefixed, so every stored link is directly usable;
    a recognized yes/no synonym is stored as its canonical word."""
    if value is None:
        return None
    if field in _URL_FIELDS and "://" not in value:
        return f"https://{value}"
    if field in YES_NO_FIELDS:
        return _YES_NO_SYNONYMS.get(value.strip().lower(), value)
    return value


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
    if field in YES_NO_FIELDS and value not in YES_NO_CHOICES:
        raise InvalidProfileValueError(
            f"'{value}' is not an answer to '{field}' (expected"
            f" {' or '.join(YES_NO_CHOICES)}; y/n are accepted too)")


def authorization_contradiction(fields: dict[str, str | None]) -> str | None:
    """The one combination of authorization answers that cannot both be
    straightforwardly true. It is not always an error (a time-limited permit
    is real), so this names the conflict for the human to resolve rather than
    refusing the answers."""
    if fields.get("authorized_in_country") == "yes" and fields.get("needs_sponsorship") == "yes":
        return ("you are authorized to work in your target country AND need visa"
                " sponsorship; eligibility gates read these as opposites, so"
                " unless a time-limited permit really makes both true, one of"
                " them is the answer you meant")
    return None
