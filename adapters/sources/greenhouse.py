"""Greenhouse boards API adapter (OC-37 §1):
GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true.

The boards API returns the whole board in one response; completeness is
validated against meta.total where present (a count mismatch is a shape
failure, not a partial commit). No vendor snapshot token: closure follows the
two-poll rule (§3).
"""

from adapters.sources.base import BaseSourceAdapter, PollPayload, require
from domain.normalization import normalize_salary, normalize_seniority


class GreenhouseAdapter(BaseSourceAdapter):
    ats_type = "greenhouse"

    def poll(self, tenant_slug: str) -> PollPayload:
        url = (f"https://boards-api.greenhouse.io/v1/boards/{tenant_slug}"
               "/jobs?content=true")
        page = self._fetcher.fetch_json(url)
        require(isinstance(page, dict) and isinstance(page.get("jobs"), list),
                "greenhouse payload lacks a 'jobs' array")
        jobs = page["jobs"]
        meta = page.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("total"), int):
            require(meta["total"] == len(jobs),
                    f"greenhouse meta.total {meta['total']} does not match"
                    f" {len(jobs)} returned jobs; truncated feed")
        for job in jobs:
            require(isinstance(job, dict) and job.get("id") is not None
                    and isinstance(job.get("title"), str),
                    "greenhouse job record fails shape validation")
        return PollPayload(pages=(page,), jobs=tuple(self._dedupe(jobs)),
                           page_count=1)

    def jobs_from_pages(self, pages: list) -> list:
        return [job for page in pages for job in page.get("jobs", [])]

    def external_id(self, raw_job) -> str:
        return str(raw_job["id"])

    def normalize(self, raw_job) -> dict:
        location = (raw_job.get("location") or {}).get("name")
        locations = [location] if isinstance(location, str) and location.strip() else []
        title = raw_job.get("title")
        return {
            "external_job_id": self.external_id(raw_job),
            "title": title,
            "seniority": normalize_seniority(title=title),
            "seniority_provenance": "greenhouse:title" if title else None,
            "description": raw_job.get("content"),
            "locations": locations,
            "location_provenance": "greenhouse:location.name" if locations else None,
            # Greenhouse states no structured remote mode on the boards API.
            "remote_mode": None,
            "remote_restriction_countries": None,
            # No structured salary payload on the boards API: absent, which
            # the compensation dimension skips auditable (§5).
            "salary": normalize_salary(None),
            "salary_provenance": None,
            "apply_url": raw_job.get("absolute_url"),
        }
