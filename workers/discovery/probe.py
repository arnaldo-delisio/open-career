"""The shared probe service (§1/§2): one healthcheck with raw-body capture,
used by the worker's probe loop and the CLI's `discover sources enable` alike.

Every received response body persists to durable storage before parsing or
status handling (the fetch layer's capture sink), so successful, malformed,
and upstream-error probes all leave byte-true, replayable evidence, and both
callers reference the same locators in their outcome records.
"""

from domain.ids import new_id


def probe_source(storage, adapter, source_id: str,
                 tenant_slug: str) -> tuple[bool, list[str]]:
    """Run one healthcheck through the adapter with per-attempt raw capture.
    Returns (passed, captured body locators)."""
    fetcher = getattr(adapter, "_fetcher", None)
    attempt_id = new_id("att")
    captured: list[str] = []

    def capture(body: bytes, status) -> None:
        locator = (f"discovery/raw/{source_id}/{attempt_id}"
                   f"/response-{len(captured) + 1:04d}.json")
        storage.write_bytes_new(locator, body)
        captured.append(locator)

    if fetcher is not None:
        fetcher.capture = capture
    try:
        ok = adapter.healthcheck(tenant_slug)
    finally:
        if fetcher is not None:
            fetcher.capture = None
    return ok, captured
