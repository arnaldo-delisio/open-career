"""Workable widget API adapter (OC-37 §1, discovery-only source):
GET apply.workable.com/api/v1/widget/accounts/{account}.

The widget endpoint offers no cursor; completeness is validated against the
'total' field, and a count mismatch degrades the poll (never a silent
partial), stated per adapter as §1 requires. Opportunities from here carry
apply_support 'none' (the extension does not fill Workable in V0).
"""

from adapters.sources.base import BaseSourceAdapter, PollPayload, require
from domain.normalization import normalize_salary, normalize_seniority


class WorkableAdapter(BaseSourceAdapter):
    ats_type = "workable"

    def poll(self, tenant_slug: str) -> PollPayload:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{tenant_slug}"
        page = self._fetcher.fetch_json(url)
        require(isinstance(page, dict) and isinstance(page.get("jobs"), list),
                "workable payload lacks a 'jobs' array")
        jobs = page["jobs"]
        total = page.get("total")
        if isinstance(total, int):
            require(total == len(jobs),
                    f"workable total {total} does not match {len(jobs)} returned"
                    " jobs; the widget endpoint offers no cursor, so this poll"
                    " cannot be completed and degrades")
        for job in jobs:
            require(isinstance(job, dict) and isinstance(job.get("shortcode"), str)
                    and isinstance(job.get("title"), str),
                    "workable job record fails shape validation")
        return PollPayload(pages=(page,), jobs=tuple(self._dedupe(jobs)),
                           page_count=1)

    def jobs_from_pages(self, pages: list) -> list:
        return [job for page in pages for job in page.get("jobs", [])]

    def external_id(self, raw_job) -> str:
        return raw_job["shortcode"]

    def normalize(self, raw_job) -> dict:
        city = raw_job.get("city")
        country = raw_job.get("country")
        parts = [p for p in (city, country) if isinstance(p, str) and p.strip()]
        locations = [", ".join(parts)] if parts else []
        remote_mode = "remote" if raw_job.get("telecommuting") is True else None
        title = raw_job.get("title")
        return {
            "external_job_id": self.external_id(raw_job),
            "title": title,
            "seniority": normalize_seniority(title=title),
            "seniority_provenance": "workable:title" if title else None,
            "description": raw_job.get("description"),
            "locations": locations,
            "location_provenance": "workable:city+country" if locations else None,
            "remote_mode": remote_mode,
            "remote_provenance": "workable:telecommuting" if remote_mode else None,
            "remote_restriction_countries": None,
            # The widget payload states no structured salary: absent (§5 skip).
            "salary": normalize_salary(None),
            "salary_provenance": None,
            "apply_url": raw_job.get("application_url") or raw_job.get("url"),
        }
