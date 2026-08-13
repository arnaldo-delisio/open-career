"""Lever postings API adapter (OC-37 §1):
GET api.lever.co/v0/postings/{site}?mode=json, paginated with skip/limit.

Pagination: fixed page size, skip advances until a short page; every page must
be a JSON array or the poll degrades and nothing commits. No vendor snapshot
token: closure follows the two-poll rule (§3).
"""

from adapters.sources.base import (
    BaseSourceAdapter,
    OversizedFeedError,
    PollPayload,
    integral_int,
    require,
)
from domain.normalization import normalize_salary, normalize_seniority

PAGE_SIZE = 100

_WORKPLACE_TO_MODE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite",
                      "onsite": "onsite"}


class LeverAdapter(BaseSourceAdapter):
    ats_type = "lever"

    def poll(self, tenant_slug: str) -> PollPayload:
        pages: list = []
        jobs: list = []
        skip = 0
        while True:
            if len(pages) >= self._max_pages:
                raise OversizedFeedError(
                    f"lever feed for '{tenant_slug}' exceeds the per-poll page"
                    f" limit ({self._max_pages}); raise config to resolve")
            url = (f"https://api.lever.co/v0/postings/{tenant_slug}"
                   f"?mode=json&limit={PAGE_SIZE}&skip={skip}")
            page = self._fetcher.fetch_json(url)
            require(isinstance(page, list), "lever payload is not a JSON array")
            for job in page:
                require(isinstance(job, dict) and isinstance(job.get("id"), str)
                        and isinstance(job.get("text"), str),
                        "lever posting record fails shape validation")
            pages.append(page)
            jobs.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        return PollPayload(pages=tuple(pages), jobs=tuple(self._dedupe(jobs)),
                           page_count=len(pages))

    def jobs_from_pages(self, pages: list) -> list:
        return [job for page in pages for job in page]

    def external_id(self, raw_job) -> str:
        return raw_job["id"]

    def normalize(self, raw_job) -> dict:
        categories = raw_job.get("categories") or {}
        country = raw_job.get("country")
        raw_locations = categories.get("allLocations")
        if not isinstance(raw_locations, list) or not raw_locations:
            raw_locations = [categories.get("location")]
        locations = []
        for loc in raw_locations:
            if not isinstance(loc, str) or not loc.strip():
                continue
            if isinstance(country, str) and country.strip() \
                    and country.lower() not in loc.lower():
                locations.append(f"{loc}, {country}")
            else:
                locations.append(loc)
        workplace = raw_job.get("workplaceType")
        remote_mode = _WORKPLACE_TO_MODE.get(workplace) if isinstance(workplace, str) else None
        title = raw_job.get("text")
        return {
            "external_job_id": self.external_id(raw_job),
            "title": title,
            "seniority": normalize_seniority(title=title),
            "seniority_provenance": "lever:text" if title else None,
            "description": raw_job.get("descriptionPlain") or raw_job.get("description"),
            "locations": locations,
            "location_provenance":
                "lever:categories.allLocations+country" if locations else None,
            "remote_mode": remote_mode,
            "remote_provenance": "lever:workplaceType" if remote_mode else None,
            "remote_restriction_countries": None,
            "salary": normalize_salary(_salary_payload(raw_job.get("salaryRange"))),
            "salary_provenance":
                "lever:salaryRange" if raw_job.get("salaryRange") else None,
            "apply_url": raw_job.get("applyUrl") or raw_job.get("hostedUrl"),
        }


def _salary_payload(salary_range) -> dict | None:
    if not isinstance(salary_range, dict):
        return None
    interval = str(salary_range.get("interval", "")).lower()
    if "year" in interval:
        period = "annual"
    elif "month" in interval:
        period = "monthly"
    elif "hour" in interval:
        period = "hourly"
    else:
        period = None
    return {
        "min": integral_int(salary_range.get("min")),
        "max": integral_int(salary_range.get("max")),
        "currency": salary_range.get("currency"),
        "period": period,
    }
