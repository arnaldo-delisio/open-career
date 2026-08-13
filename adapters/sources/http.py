"""The one HTTP boundary for discovery source adapters (OC-37 §1, OC-1).

Adapters may call only the whitelisted public API hosts below: the whitelist
is a tested constant, and a URL outside it is a refused fetch, never a
warning. Politeness is implemented, not prose: an identified User-Agent naming
the project and repo URL, a per-host minimum interval, and exponential backoff
on 429/5xx. Stdlib urllib only; the domain never imports this module.
"""

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

# OC-1: the five verified public job-board API hosts, and nothing else.
ALLOWED_HOSTS: frozenset[str] = frozenset({
    "boards-api.greenhouse.io",
    "api.lever.co",
    "api.ashbyhq.com",
    "apply.workable.com",
    "api.smartrecruiters.com",
})

# Identified, as the design requires: project name plus repo URL.
USER_AGENT = "open-career-discovery (+https://github.com/arnaldo-delisio/open-career)"

# Conservative defaults, config via constructor (§4 records them on the run).
DEFAULT_MIN_INTERVAL_S = 2.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT_S = 30
MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _header(headers: dict, name: str) -> str | None:
    """Case-insensitive header lookup over a plain dict."""
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


class RefusedHostError(RuntimeError):
    """The URL's host is outside the OC-1 whitelist; the fetch never ran."""


class FetchError(RuntimeError):
    """The fetch failed after retries, or the body was not valid JSON."""


class NotFoundError(FetchError):
    """HTTP 404: an invalid tenant slug 404s cleanly (healthcheck false)."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Automatic redirects are disabled (OC-1): a redirect is followed only
    after its target host passes the whitelist check in HttpFetcher."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _default_transport(url: str, headers: dict,
                       timeout: float) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


class HttpFetcher:
    """Whitelisted, rate-limited, retrying JSON GETs. transport, sleep, and
    clock are injectable so the test suite never touches the network."""

    def __init__(self, min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 transport=None, sleep=time.sleep, clock=time.monotonic):
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        self._transport = transport or _default_transport
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: dict[str, float] = {}
        # Requests actually issued (retries included): the accounting unit the
        # worker charges fetch spend from, so failed and oversized polls still
        # consume budget at the request boundary.
        self.request_count = 0
        # Raw-capture sink (§1: raw responses are snapshotted before any
        # parsing): when set, EVERY received response body streams here as
        # capture(body, status), before status handling or JSON decoding,
        # non-2xx included, so parse, validation, and upstream failures all
        # leave the fetched evidence durable and replayable.
        self.capture = None

    def fetch_json(self, url: str):
        """GET the URL and parse the JSON body. Refuses non-whitelisted hosts,
        waits out the per-host minimum interval, and backs off exponentially
        on 429 and 5xx (2s, 4s, 8s) before giving up with FetchError.

        Redirects are never followed automatically: a redirect target is
        fetched only after its own host passes the whitelist (OC-1), with a
        bounded depth."""
        host = self._require_allowed(url)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        attempt = 0
        redirects = 0
        while True:
            self._respect_interval(host)
            try:
                response = self._transport(url, headers, self._timeout_s)
                # Transports may return (status, body) or (status, body,
                # headers); redirect handling needs the third element.
                status, body = response[0], response[1]
                response_headers = response[2] if len(response) > 2 else {}
            except OSError as e:
                status, body, response_headers = None, None, {}
                error: Exception | None = e
            else:
                error = None
            self.request_count += 1
            self._last_request_at[host] = self._clock()
            if body is not None and self.capture is not None:
                # Every received body streams to the sink BEFORE status
                # handling or decoding, non-2xx included (retryable and
                # terminal): error documents are evidence too.
                self.capture(body, status)
            if status in _REDIRECT_STATUSES:
                location = _header(response_headers, "Location")
                if location is None:
                    raise FetchError(f"redirect without Location from {url}")
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise FetchError(f"more than {MAX_REDIRECTS} redirects from {url}")
                url = urljoin(url, location)
                host = self._require_allowed(url)  # refused BEFORE any request
                continue
            if status == 404:
                raise NotFoundError(f"404 for {url}")
            if status is not None and 200 <= status < 300:
                try:
                    return json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raise FetchError(f"non-JSON body from {url}: {e}") from e
            retryable = error is not None or status == 429 or (
                status is not None and status >= 500)
            attempt += 1
            if not retryable or attempt > self._max_retries:
                detail = f"HTTP {status}" if error is None else f"{error}"
                raise FetchError(f"fetch failed for {url}: {detail}")
            self._sleep(2 ** attempt)  # 2, 4, 8

    @staticmethod
    def _require_allowed(url: str) -> str:
        host = urlparse(url).hostname
        if host not in ALLOWED_HOSTS:
            # The host may come from a fetched redirect target: rendered as an
            # attributed quoted value, never interpolated bare.
            raise RefusedHostError(
                f'request target host "{host}" is outside the OC-1 whitelist;'
                f" refused (allowed: {sorted(ALLOWED_HOSTS)})")
        return host

    def _respect_interval(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is None:
            return
        elapsed = self._clock() - last
        if elapsed < self._min_interval_s:
            self._sleep(self._min_interval_s - elapsed)
