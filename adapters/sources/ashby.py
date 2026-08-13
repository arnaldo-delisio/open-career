"""Ashby posting API adapter (OC-37 §1):
GET api.ashbyhq.com/posting-api/job-board/{client}?includeCompensation=true.

One response carries the whole board. Compensation arrives structured in
summaryComponents; only a clean Salary component with a supported interval
normalizes, anything else is absent (auditable skip, §5). No vendor snapshot
token: closure follows the two-poll rule (§3).
"""

from adapters.sources.base import (
    BaseSourceAdapter,
    PollPayload,
    integral_int,
    require,
)
from domain.normalization import normalize_salary, normalize_seniority

_INTERVAL_TO_PERIOD = {"1 YEAR": "annual", "1 MONTH": "monthly", "1 HOUR": "hourly"}


class AshbyAdapter(BaseSourceAdapter):
    ats_type = "ashby"

    def poll(self, tenant_slug: str) -> PollPayload:
        url = (f"https://api.ashbyhq.com/posting-api/job-board/{tenant_slug}"
               "?includeCompensation=true")
        page = self._fetcher.fetch_json(url)
        require(isinstance(page, dict) and isinstance(page.get("jobs"), list),
                "ashby payload lacks a 'jobs' array")
        jobs = page["jobs"]
        for job in jobs:
            require(isinstance(job, dict) and isinstance(job.get("id"), str)
                    and isinstance(job.get("title"), str),
                    "ashby job record fails shape validation")
        return PollPayload(pages=(page,), jobs=tuple(self._dedupe(jobs)),
                           page_count=1)

    def jobs_from_pages(self, pages: list) -> list:
        return [job for page in pages for job in page.get("jobs", [])]

    def external_id(self, raw_job) -> str:
        return raw_job["id"]

    def normalize(self, raw_job) -> dict:
        locations = []
        primary = raw_job.get("location")
        if isinstance(primary, str) and primary.strip():
            locations.append(primary)
        for secondary in raw_job.get("secondaryLocations") or []:
            loc = secondary.get("location") if isinstance(secondary, dict) else secondary
            if isinstance(loc, str) and loc.strip():
                locations.append(loc)
        remote_mode = "remote" if raw_job.get("isRemote") is True else None
        title = raw_job.get("title")
        compensation = raw_job.get("compensation")
        return {
            "external_job_id": self.external_id(raw_job),
            "title": title,
            "seniority": normalize_seniority(title=title),
            "seniority_provenance": "ashby:title" if title else None,
            "description": raw_job.get("descriptionPlain") or raw_job.get("descriptionHtml"),
            "locations": locations,
            "location_provenance":
                "ashby:location+secondaryLocations" if locations else None,
            "remote_mode": remote_mode,
            "remote_provenance": "ashby:isRemote" if remote_mode else None,
            "remote_restriction_countries": None,
            "salary": normalize_salary(_salary_payload(compensation)),
            "salary_provenance":
                "ashby:compensation.summaryComponents" if compensation else None,
            "apply_url": raw_job.get("applyUrl") or raw_job.get("jobUrl"),
        }


def _salary_payload(compensation) -> dict | None:
    if not isinstance(compensation, dict):
        return None
    components = compensation.get("summaryComponents")
    if not isinstance(components, list):
        return None
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("compensationType", "")).lower() != "salary":
            continue
        return {
            "min": integral_int(component.get("minValue")),
            "max": integral_int(component.get("maxValue")),
            "currency": component.get("currencyCode"),
            "period": _INTERVAL_TO_PERIOD.get(component.get("interval")),
        }
    return None
