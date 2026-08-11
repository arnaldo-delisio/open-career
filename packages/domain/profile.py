"""User profile: the closed canonical field set (OC-29) and write validation.

28 profile-derivable fields, one JSON document in the single-row user_profile
table. Anything not answerable from the profile alone is a custom question for
the Resolution/authored-answer lane, never a schema field. The set is validated
here, in the domain, not frozen into DDL.
"""

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


def validate_profile_field(field: str) -> None:
    """The set is closed: an unknown field is an error, never a silent write."""
    if field not in CANONICAL_PROFILE_FIELDS:
        raise UnknownProfileFieldError(
            f"unknown profile field '{field}'; canonical fields: "
            + ", ".join(sorted(CANONICAL_PROFILE_FIELDS))
        )
