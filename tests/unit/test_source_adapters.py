"""Source adapters (OC-37 §1): the OC-1 host whitelist as a tested constant
with refusal outside it, identified User-Agent, per-host interval and backoff,
per-vendor pagination with the completeness contract (schema-valid empty feed
= complete success), collision policy, and normalization into the canonical
shapes with provenance. Fixtures are captured-shape JSON; no live network."""

import json
from pathlib import Path

import pytest

from adapters.sources.ashby import AshbyAdapter
from adapters.sources.base import (
    AdapterDegradedError,
    OversizedFeedError,
    SnapshotCollisionError,
)
from adapters.sources.greenhouse import GreenhouseAdapter
from adapters.sources.http import (
    ALLOWED_HOSTS,
    USER_AGENT,
    FetchError,
    HttpFetcher,
    NotFoundError,
    RefusedHostError,
)
from adapters.sources.lever import LeverAdapter
from adapters.sources.smartrecruiters import SmartRecruitersAdapter
from adapters.sources.workable import WorkableAdapter
from adapters.sources import build_adapters

FIXTURES = Path(__file__).parent.parent / "fixtures" / "source_api"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeTransport:
    """Canned (status, body) responses keyed by URL substring, capturing every
    request's URL and headers."""

    def __init__(self, responses: dict):
        self._responses = responses
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url: str, headers: dict, timeout: float):
        self.requests.append((url, headers))
        for key, response in self._responses.items():
            if key in url:
                status, body = response.pop(0) if isinstance(response, list) \
                    else response
                return status, json.dumps(body).encode() \
                    if not isinstance(body, bytes) else body
        return 404, b"{}"


def make_fetcher(responses: dict, **kwargs):
    transport = FakeTransport(responses)
    fetcher = HttpFetcher(transport=transport, sleep=lambda _s: None,
                          clock=lambda: 0.0, **kwargs)
    return fetcher, transport


# --------------------------------------------------------------- OC-1 / http

def test_whitelist_is_exactly_the_five_public_api_hosts():
    assert ALLOWED_HOSTS == {
        "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com",
        "apply.workable.com", "api.smartrecruiters.com"}


def test_url_outside_the_whitelist_is_a_refused_fetch_not_a_warning():
    fetcher, transport = make_fetcher({})
    with pytest.raises(RefusedHostError):
        fetcher.fetch_json("https://www.linkedin.com/jobs")
    with pytest.raises(RefusedHostError):
        fetcher.fetch_json("https://boards.greenhouse.io/acme")  # HTML host, not API
    assert transport.requests == []  # never left the process


def test_user_agent_names_the_project_and_repo():
    assert "open-career" in USER_AGENT
    assert "github.com/arnaldo-delisio/open-career" in USER_AGENT
    fetcher, transport = make_fetcher({"greenhouse": (200, {"jobs": []})})
    fetcher.fetch_json("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert transport.requests[0][1]["User-Agent"] == USER_AGENT


def test_per_host_minimum_interval_sleeps_between_requests():
    sleeps = []
    clock_value = [0.0]
    transport = FakeTransport({"lever": (200, [])})
    fetcher = HttpFetcher(min_interval_s=2.0, transport=transport,
                          sleep=sleeps.append, clock=lambda: clock_value[0])
    fetcher.fetch_json("https://api.lever.co/v0/postings/acme?mode=json")
    clock_value[0] = 0.5  # half a second later
    fetcher.fetch_json("https://api.lever.co/v0/postings/acme?mode=json&skip=100")
    assert sleeps and sleeps[0] == pytest.approx(1.5)


def test_backoff_retries_429_then_succeeds():
    sleeps = []
    transport = FakeTransport({"lever": [(429, {}), (429, {}), (200, [])]})
    fetcher = HttpFetcher(transport=transport, sleep=sleeps.append,
                          clock=lambda: 0.0, min_interval_s=0)
    assert fetcher.fetch_json("https://api.lever.co/v0/postings/acme") == []
    assert sleeps == [2, 4]  # exponential


def test_backoff_gives_up_after_max_retries():
    transport = FakeTransport({"lever": [(500, {})] * 10})
    fetcher = HttpFetcher(transport=transport, sleep=lambda _s: None,
                          clock=lambda: 0.0, min_interval_s=0, max_retries=3)
    with pytest.raises(FetchError):
        fetcher.fetch_json("https://api.lever.co/v0/postings/acme")
    assert len(transport.requests) == 4  # initial + 3 retries


def test_404_raises_not_found():
    fetcher, _ = make_fetcher({})
    with pytest.raises(NotFoundError):
        fetcher.fetch_json("https://api.lever.co/v0/postings/nosuch")


def test_redirect_to_a_non_whitelisted_host_is_refused_before_any_request():
    """Codex r5 finding 1: automatic redirects are disabled; a whitelisted
    host 302ing to a foreign host is refused BEFORE any request reaches the
    target (OC-1 cannot be bypassed by redirect)."""
    class RedirectingTransport:
        def __init__(self):
            self.requests: list[str] = []

        def __call__(self, url, headers, timeout):
            self.requests.append(url)
            return 302, b"", {"Location": "https://evil.example/steal"}

    transport = RedirectingTransport()
    fetcher = HttpFetcher(transport=transport, sleep=lambda _s: None,
                          clock=lambda: 0.0, min_interval_s=0)
    with pytest.raises(RefusedHostError):
        fetcher.fetch_json("https://api.lever.co/v0/postings/acme")
    assert transport.requests == ["https://api.lever.co/v0/postings/acme"]
    assert not any("evil.example" in u for u in transport.requests)


def test_redirect_between_whitelisted_hosts_is_followed_with_bounded_depth():
    class Transport:
        def __init__(self):
            self.requests: list[str] = []

        def __call__(self, url, headers, timeout):
            self.requests.append(url)
            if "old-path" in url:
                return 301, b"", {"location": "/v0/postings/acme?mode=json"}
            return 200, json.dumps([]).encode(), {}

    transport = Transport()
    fetcher = HttpFetcher(transport=transport, sleep=lambda _s: None,
                          clock=lambda: 0.0, min_interval_s=0)
    assert fetcher.fetch_json("https://api.lever.co/old-path") == []
    assert len(transport.requests) == 2  # relative Location resolved, followed

    class LoopingTransport:
        def __call__(self, url, headers, timeout):
            return 302, b"", {"Location": url + "x"}

    looping = HttpFetcher(transport=LoopingTransport(), sleep=lambda _s: None,
                          clock=lambda: 0.0, min_interval_s=0)
    with pytest.raises(FetchError, match="redirects"):
        looping.fetch_json("https://api.lever.co/loop")


# ---------------------------------------------------------------- greenhouse

def test_greenhouse_poll_and_normalize():
    fetcher, transport = make_fetcher(
        {"boards-api.greenhouse.io": (200, fixture("greenhouse_jobs.json"))})
    adapter = GreenhouseAdapter(fetcher)
    payload = adapter.poll("acme")
    assert payload.page_count == 1
    assert len(payload.jobs) == 2
    assert "content=true" in transport.requests[0][0]
    normalized = adapter.normalize(payload.jobs[0])
    assert normalized["external_job_id"] == "4455667788"
    assert normalized["title"] == "Senior Backend Engineer"
    assert normalized["seniority"] == "senior"
    assert normalized["seniority_provenance"] == "greenhouse:title"
    assert normalized["locations"] == ["Milan, Italy"]
    assert normalized["location_provenance"] == "greenhouse:location.name"
    assert normalized["salary"] is None  # boards API states no salary: skip
    assert normalized["apply_url"].endswith("/4455667788")
    assert adapter.apply_support() == "extension"


def test_greenhouse_total_mismatch_degrades():
    body = fixture("greenhouse_jobs.json")
    body["meta"]["total"] = 5
    fetcher, _ = make_fetcher({"boards-api.greenhouse.io": (200, body)})
    with pytest.raises(AdapterDegradedError):
        GreenhouseAdapter(fetcher).poll("acme")


def test_schema_valid_empty_feed_is_a_complete_successful_snapshot():
    fetcher, _ = make_fetcher(
        {"boards-api.greenhouse.io": (200, {"jobs": [], "meta": {"total": 0}})})
    payload = GreenhouseAdapter(fetcher).poll("acme")
    assert payload.jobs == ()
    assert json.loads(payload.completion_json())["complete"] is True


def test_error_document_degrades():
    fetcher, _ = make_fetcher(
        {"boards-api.greenhouse.io": (200, {"error": "board not found"})})
    with pytest.raises(AdapterDegradedError):
        GreenhouseAdapter(fetcher).poll("acme")


def test_healthcheck_true_on_valid_board_false_on_404():
    fetcher, _ = make_fetcher(
        {"boards-api.greenhouse.io/v1/boards/acme": (200, fixture("greenhouse_jobs.json"))})
    adapter = GreenhouseAdapter(fetcher)
    assert adapter.healthcheck("acme") is True
    assert adapter.healthcheck("nosuchtenant") is False


# --------------------------------------------------------------------- lever

def test_lever_pagination_fetches_all_pages():
    page1 = [dict(fixture("lever_postings.json")[0], id=f"job-{i}")
             for i in range(100)]
    page2 = fixture("lever_postings.json")
    fetcher, transport = make_fetcher({"skip=0": (200, page1),
                                       "skip=100": (200, page2)})
    payload = LeverAdapter(fetcher).poll("acme")
    assert payload.page_count == 2
    assert len(payload.jobs) == 102
    assert "skip=100" in transport.requests[1][0]


def test_lever_normalize_salary_location_seniority():
    adapter = LeverAdapter(None)
    staff, junior = fixture("lever_postings.json")
    n = adapter.normalize(staff)
    assert n["seniority"] == "staff_plus"
    assert n["locations"] == ["Milan, IT"]
    assert n["remote_mode"] == "hybrid"
    assert n["salary"] == {"min": 90000, "max": 120000, "currency": "EUR",
                           "period": "annual", "equity_only": False}
    assert n["salary_provenance"] == "lever:salaryRange"
    n2 = adapter.normalize(junior)
    assert n2["seniority"] == "junior"
    assert n2["remote_mode"] == "remote"
    assert n2["salary"] is None
    assert adapter.apply_support() == "extension"


def test_lever_fractional_salary_normalizes_to_absent():
    job = dict(fixture("lever_postings.json")[0])
    job["salaryRange"] = {"min": 90000.5, "max": 120000, "currency": "EUR",
                          "interval": "per-year-salary"}
    assert LeverAdapter(None).normalize(job)["salary"] is None


def test_lever_oversized_feed_is_auditable_never_partial():
    full_page = [dict(fixture("lever_postings.json")[0], id=f"job-{i}")
                 for i in range(100)]
    fetcher, _ = make_fetcher({"api.lever.co": (200, full_page)})
    with pytest.raises(OversizedFeedError):
        LeverAdapter(fetcher, max_pages_per_poll=1).poll("acme")


def test_lever_shape_invalid_page_degrades():
    fetcher, _ = make_fetcher({"api.lever.co": (200, {"not": "an array"})})
    with pytest.raises(AdapterDegradedError):
        LeverAdapter(fetcher).poll("acme")


# --------------------------------------------------------------------- ashby

def test_ashby_poll_and_normalize_with_compensation():
    fetcher, transport = make_fetcher(
        {"api.ashbyhq.com": (200, fixture("ashby_board.json"))})
    adapter = AshbyAdapter(fetcher)
    payload = adapter.poll("acme")
    assert "includeCompensation=true" in transport.requests[0][0]
    engineer, sales = payload.jobs
    n = adapter.normalize(engineer)
    # 70000.0 arrives as an integral float and converts deterministically.
    assert n["salary"] == {"min": 70000, "max": 90000, "currency": "EUR",
                           "period": "annual", "equity_only": False}
    assert n["locations"] == ["Amsterdam, Netherlands", "Remote - Netherlands"]
    assert n["remote_mode"] is None
    n2 = adapter.normalize(sales)
    assert n2["remote_mode"] == "remote"
    assert n2["seniority"] == "director_plus"
    assert n2["salary"] is None
    assert adapter.apply_support() == "extension"


# ------------------------------------------------------------------ workable

def test_workable_poll_normalize_and_discovery_only():
    fetcher, _ = make_fetcher(
        {"apply.workable.com": (200, fixture("workable_account.json"))})
    adapter = WorkableAdapter(fetcher)
    payload = adapter.poll("acme")
    devops, csm = payload.jobs
    n = adapter.normalize(devops)
    assert n["external_job_id"] == "3A1B2C"
    assert n["locations"] == ["Turin, Italy"]
    assert n["remote_mode"] is None
    n2 = adapter.normalize(csm)
    assert n2["remote_mode"] == "remote"
    assert adapter.apply_support() == "none"  # discovery-only source (§1)


def test_workable_total_mismatch_degrades():
    body = fixture("workable_account.json")
    body["total"] = 50
    fetcher, _ = make_fetcher({"apply.workable.com": (200, body)})
    with pytest.raises(AdapterDegradedError):
        WorkableAdapter(fetcher).poll("acme")


# ------------------------------------------------------------ smartrecruiters

def test_smartrecruiters_poll_normalize_structured_seniority():
    fetcher, _ = make_fetcher(
        {"api.smartrecruiters.com": (200, fixture("smartrecruiters_postings.json"))})
    adapter = SmartRecruitersAdapter(fetcher)
    payload = adapter.poll("Acme1")
    frontend, director = payload.jobs
    n = adapter.normalize(frontend)
    assert n["seniority"] == "mid"  # associate -> mid, structured field wins
    assert n["seniority_provenance"] == "smartrecruiters:experienceLevel.id"
    assert n["locations"] == ["Rome, IT"]
    assert n["apply_url"] == "https://jobs.smartrecruiters.com/Acme1/744000012345678"
    n2 = adapter.normalize(director)
    assert n2["seniority"] == "director_plus"
    assert n2["remote_mode"] == "remote"
    assert adapter.apply_support() == "none"  # discovery-only source (§1)


def test_smartrecruiters_totalfound_mismatch_degrades():
    body = fixture("smartrecruiters_postings.json")
    body["totalFound"] = 7
    fetcher, _ = make_fetcher({"api.smartrecruiters.com": (200, body)})
    with pytest.raises(AdapterDegradedError):
        SmartRecruitersAdapter(fetcher).poll("Acme1")


EMPTY_POSTINGS = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}


def smartrecruiters_transport(*, tenant_exists: bool, postings=None):
    """The vendor's real shapes (verified live 2026-08-13): the postings
    collection answers 200 totalFound 0 for ANY company id, real or invented,
    while the departments resource 404s on an unknown id."""
    return {
        "/departments": (200, {"totalFound": 1, "content": [{"id": 1}]})
        if tenant_exists else (404, {"httpCode": 404, "code": "RESOURCE_NOT_FOUND"}),
        "/postings": (200, postings if postings is not None else EMPTY_POSTINGS),
    }


def test_smartrecruiters_probe_rejects_a_bogus_slug_the_postings_api_accepts():
    """The postings collection cannot verify tenant existence, so the probe
    reads the departments resource: an invented slug fails the healthcheck
    even though its postings response is a valid 200."""
    fetcher, transport = make_fetcher(
        smartrecruiters_transport(tenant_exists=False))
    assert SmartRecruitersAdapter(fetcher).healthcheck("nosuchtenant") is False
    assert all("/departments" in url for url, _headers in transport.requests)


def test_smartrecruiters_probe_passes_a_real_tenant_with_or_without_postings():
    """A real tenant verifies on positive evidence of existence, which is
    independent of whether it currently has open postings."""
    with_postings, _ = make_fetcher(smartrecruiters_transport(
        tenant_exists=True, postings=fixture("smartrecruiters_postings.json")))
    assert SmartRecruitersAdapter(with_postings).healthcheck("Acme1") is True
    empty, _ = make_fetcher(smartrecruiters_transport(tenant_exists=True))
    assert SmartRecruitersAdapter(empty).healthcheck("Acme1") is True


@pytest.mark.parametrize("body", [
    {"error": "company not found"},  # error document served with a 200
    {"content": []},  # partial: the collection shape lacks totalFound
    {"totalFound": 3},  # partial: no content list
    {"totalFound": True, "content": []},  # a bool is not a count
])
def test_smartrecruiters_probe_fails_on_a_200_that_is_not_the_collection_shape(body):
    """Only a schema-valid departments collection is positive evidence of
    tenant existence; a partial or error body served with a 200 must not
    enable a candidate."""
    fetcher, _ = make_fetcher({"/departments": (200, body)})
    assert SmartRecruitersAdapter(fetcher).healthcheck("Acme1") is False


def test_smartrecruiters_probe_accepts_a_real_tenant_with_no_departments():
    """totalFound 0 is a legitimate departments response for a real tenant
    (observed live), so it verifies."""
    fetcher, _ = make_fetcher({"/departments": (200, {"totalFound": 0, "content": []})})
    assert SmartRecruitersAdapter(fetcher).healthcheck("Acme1") is True


def test_smartrecruiters_zero_posting_poll_is_a_complete_successful_snapshot():
    """Probing is not polling: for an already verified tenant, a schema-valid
    zero-posting feed stays a complete successful snapshot (§1)."""
    fetcher, _ = make_fetcher({"/postings": (200, EMPTY_POSTINGS)})
    payload = SmartRecruitersAdapter(fetcher).poll("Acme1")
    assert payload.jobs == ()
    assert json.loads(payload.completion_json())["complete"] is True


# ----------------------------------------------------------------- collision

def test_byte_identical_duplicate_records_collapse():
    body = fixture("greenhouse_jobs.json")
    body["jobs"].append(json.loads(json.dumps(body["jobs"][0])))
    body["meta"]["total"] = 3
    fetcher, _ = make_fetcher({"boards-api.greenhouse.io": (200, body)})
    payload = GreenhouseAdapter(fetcher).poll("acme")
    assert len(payload.jobs) == 2  # deterministic collapse


def test_materially_different_collision_degrades_the_snapshot():
    body = fixture("greenhouse_jobs.json")
    clone = json.loads(json.dumps(body["jobs"][0]))
    clone["title"] = "Completely Different Title"
    body["jobs"].append(clone)
    body["meta"]["total"] = 3
    fetcher, _ = make_fetcher({"boards-api.greenhouse.io": (200, body)})
    with pytest.raises(SnapshotCollisionError):
        GreenhouseAdapter(fetcher).poll("acme")


# ------------------------------------------------------------------- factory

def test_build_adapters_covers_all_five_ats_types():
    adapters = build_adapters(fetcher=make_fetcher({})[0])
    assert set(adapters) == {"greenhouse", "lever", "ashby", "workable",
                             "smartrecruiters"}
