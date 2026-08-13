"""SmartRecruiters postings API adapter (OC-37 §1, discovery-only source):
GET api.smartrecruiters.com/v1/companies/{id}/postings, offset pagination.

Completeness: pages advance by offset until totalFound is covered; a page
count disagreeing with totalFound degrades the poll. experienceLevel.id is a
structured seniority field, mapped through a tested constant. Opportunities
carry apply_support 'none' (no V0 fill).

Probing is not polling. The postings collection cannot answer "does this
tenant exist": SmartRecruiters serves HTTP 200 with totalFound 0 for any
company id, real or invented, so the §2 verification-before-enablement probe
uses the departments resource instead, which 404s on an unknown id. Polling is
unaffected: for an already verified tenant a schema-valid zero-posting
response stays a complete successful snapshot (§1).
"""

from adapters.sources.base import (
    BaseSourceAdapter,
    OversizedFeedError,
    PollPayload,
    require,
)
from adapters.sources.http import FetchError, RefusedHostError
from domain.normalization import normalize_salary, normalize_seniority

PAGE_SIZE = 100

# Structured ATS seniority (§5): SmartRecruiters experienceLevel.id -> band.
EXPERIENCE_LEVEL_TO_BAND: dict[str, str] = {
    "internship": "intern",
    "entry_level": "junior",
    "associate": "mid",
    "mid_senior_level": "senior",
    "director": "director_plus",
    "executive": "director_plus",
}


class SmartRecruitersAdapter(BaseSourceAdapter):
    ats_type = "smartrecruiters"

    def poll(self, tenant_slug: str) -> PollPayload:
        # Remembered for apply_url construction when a posting record carries
        # no company.identifier; set before any normalize call so material
        # fingerprints stay deterministic within and across polls.
        self._tenant = tenant_slug
        pages: list = []
        jobs: list = []
        offset = 0
        total = None
        while True:
            if len(pages) >= self._max_pages:
                raise OversizedFeedError(
                    f"smartrecruiters feed for '{tenant_slug}' exceeds the"
                    f" per-poll page limit ({self._max_pages}); raise config")
            url = (f"https://api.smartrecruiters.com/v1/companies/{tenant_slug}"
                   f"/postings?limit={PAGE_SIZE}&offset={offset}")
            page = self._fetcher.fetch_json(url)
            require(isinstance(page, dict) and isinstance(page.get("content"), list)
                    # bool subclasses int, so a JSON true would otherwise pass
                    # as a count and satisfy the completeness check below.
                    and isinstance(page.get("totalFound"), int)
                    and not isinstance(page.get("totalFound"), bool),
                    "smartrecruiters payload lacks 'content'/'totalFound'")
            if total is None:
                total = page["totalFound"]
            else:
                # The feed changed mid-pagination: pages no longer describe
                # one consistent set, so the poll degrades and commits
                # nothing (§1 completeness contract).
                require(page["totalFound"] == total,
                        f"smartrecruiters totalFound changed mid-pagination"
                        f" ({total} -> {page['totalFound']}); inconsistent"
                        " feed, poll degraded")
            for job in page["content"]:
                require(isinstance(job, dict) and job.get("id") is not None
                        and isinstance(job.get("name"), str),
                        "smartrecruiters posting record fails shape validation")
            pages.append(page)
            jobs.extend(page["content"])
            offset += PAGE_SIZE
            if offset >= total or not page["content"]:
                break
        require(len(jobs) == total,
                f"smartrecruiters returned {len(jobs)} postings against"
                f" totalFound {total}; truncated feed")
        return PollPayload(pages=tuple(pages), jobs=tuple(self._dedupe(jobs)),
                           page_count=len(pages))

    def healthcheck(self, tenant_slug: str) -> bool:
        """Tenant existence, verified against the departments resource rather
        than the postings collection (see module docstring). Verified live
        2026-08-13: a real tenant answers 200 whether or not it has open
        postings, an unknown id answers 404 RESOURCE_NOT_FOUND, while the
        postings collection answers 200 totalFound 0 for both. A non-404
        failure (refused host, malformed body, upstream error) is a failed
        check like any other, so the source stays a candidate and ages into a
        later re-probe."""
        url = (f"https://api.smartrecruiters.com/v1/companies/{tenant_slug}"
               f"/departments")
        try:
            body = self._fetcher.fetch_json(url)
        except (FetchError, RefusedHostError, ValueError):
            return False
        # A tenant that exists returns the full collection shape, totalFound
        # included and legitimately 0 (verified live on real tenants with no
        # departments configured). Anything less is not positive evidence:
        # an error document or a partial body served with a 200 must not
        # enable a candidate, which is the failure this probe exists to close.
        return (isinstance(body, dict) and isinstance(body.get("content"), list)
                and isinstance(body.get("totalFound"), int)
                and not isinstance(body.get("totalFound"), bool)
                and body["totalFound"] >= 0)

    def jobs_from_pages(self, pages: list) -> list:
        return [job for page in pages for job in page.get("content", [])]

    def external_id(self, raw_job) -> str:
        return str(raw_job["id"])

    def normalize(self, raw_job) -> dict:
        location = raw_job.get("location") or {}
        city = location.get("city")
        country = location.get("country")
        parts = []
        if isinstance(city, str) and city.strip():
            parts.append(city)
        if isinstance(country, str) and country.strip():
            parts.append(country.upper() if len(country.strip()) == 2 else country)
        locations = [", ".join(parts)] if parts else []
        remote_mode = "remote" if location.get("remote") is True else None
        title = raw_job.get("name")
        level_id = (raw_job.get("experienceLevel") or {}).get("id")
        structured_band = EXPERIENCE_LEVEL_TO_BAND.get(level_id)
        seniority = normalize_seniority(structured=structured_band, title=title)
        company = raw_job.get("company") or {}
        company_identifier = company.get("identifier") or getattr(self, "_tenant", None)
        apply_url = (f"https://jobs.smartrecruiters.com/{company_identifier}"
                     f"/{raw_job['id']}") if company_identifier else None
        return {
            "external_job_id": self.external_id(raw_job),
            "title": title,
            "seniority": seniority,
            "seniority_provenance": ("smartrecruiters:experienceLevel.id"
                                     if structured_band else
                                     ("smartrecruiters:name" if seniority else None)),
            # The postings list carries no description body; pursue-time work
            # fetches the posting detail (out of discovery scope, OC-23).
            "description": None,
            "locations": locations,
            "location_provenance":
                "smartrecruiters:location.city+country" if locations else None,
            "remote_mode": remote_mode,
            "remote_provenance": "smartrecruiters:location.remote" if remote_mode else None,
            "remote_restriction_countries": None,
            "salary": normalize_salary(None),
            "salary_provenance": None,
            "apply_url": apply_url,
        }
