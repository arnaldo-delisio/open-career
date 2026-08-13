"""Shared source-adapter plumbing (OC-37 §1/§3): the poll-completeness
contract, page-cost accounting, and per-snapshot external-id validation.

A poll returns every page or nothing: a truncated, partial, or shape-invalid
response raises AdapterDegradedError and nothing commits. A schema-valid
zero-posting feed is a complete successful snapshot, not a failure. Within one
snapshot, external job ids must be unique: byte-identical repeated records
collapse deterministically; one id carrying materially different payloads
degrades and rejects the snapshot (SnapshotCollisionError).
"""

import hashlib
import json
from dataclasses import dataclass

from adapters.sources.http import FetchError, RefusedHostError
from domain.discovery import apply_support_for
from domain.ports import SourceAdapter

# Hard per-poll request limit (§1, config): an in-flight poll may finish past
# the run fetch cap, but never past this; exceeding it is the auditable
# 'oversized' state, no snapshot commits.
DEFAULT_MAX_PAGES_PER_POLL = 200


class AdapterDegradedError(RuntimeError):
    """The poll cannot commit: empty body, error document, shape-invalid
    payload, or a failed fetch. Reserved for adapter and payload failures;
    a budget deferral is not degradation (§1)."""


class SnapshotCollisionError(AdapterDegradedError):
    """One external id carried materially different payloads in a single
    snapshot; the snapshot commits nothing (§3)."""


class OversizedFeedError(RuntimeError):
    """The feed exceeded the hard per-poll page limit; auditable 'oversized'
    state, resolved by raising config, never a silent partial (§1)."""


@dataclass(frozen=True)
class PollPayload:
    """One complete poll: raw pages (the replayable evidence §1 snapshots),
    the deduplicated raw job records, and the page cost for admission
    accounting. raw_text is the canonical stored form; content_hash keys it."""

    pages: tuple
    jobs: tuple
    page_count: int
    remote_version_token: str | None = None

    @property
    def raw_text(self) -> str:
        return json.dumps({"pages": list(self.pages)}, ensure_ascii=False)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.raw_text.encode()).hexdigest()

    def completion_json(self) -> str:
        return json.dumps({"pages": self.page_count, "complete": True,
                           "postings": len(self.jobs)}, sort_keys=True)


def dedupe_jobs(raw_jobs: list, id_of, material_of) -> list:
    """Per-snapshot external-id uniqueness (§3): byte-identical repeats (by
    canonical JSON of the raw record) collapse; one id with materially
    different normalized payloads raises SnapshotCollisionError."""
    seen: dict[str, tuple[str, dict]] = {}
    out = []
    for raw in raw_jobs:
        external_id = id_of(raw)
        canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        if external_id not in seen:
            seen[external_id] = (canonical, material_of(raw))
            out.append(raw)
            continue
        prior_canonical, prior_material = seen[external_id]
        if canonical == prior_canonical:
            continue  # byte-identical repeat: deterministic collapse
        if material_of(raw) != prior_material:
            # The id is fetched data: rendered as an attributed quoted value.
            raise SnapshotCollisionError(
                f'posting-supplied external id "{external_id}" appears with'
                " materially different payloads in one snapshot; snapshot"
                " rejected")
        # Same material content, different bytes (e.g. field order): the first
        # record is kept deterministically.
    return out


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise AdapterDegradedError(reason)


def description_hash(text: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return hashlib.sha256(text.encode()).hexdigest()


class BaseSourceAdapter(SourceAdapter):
    """Common shape for the five adapters: list_jobs is poll().jobs; each
    vendor implements poll (completeness enforced), jobs_from_pages (replay
    from a stored raw snapshot), normalize, and healthcheck."""

    ats_type: str = ""

    def __init__(self, fetcher, max_pages_per_poll: int = DEFAULT_MAX_PAGES_PER_POLL):
        self._fetcher = fetcher
        self._max_pages = max_pages_per_poll

    def list_jobs(self, tenant_slug: str) -> list:
        return list(self.poll(tenant_slug).jobs)

    def poll(self, tenant_slug: str) -> PollPayload:
        raise NotImplementedError

    def jobs_from_pages(self, pages: list) -> list:
        """Raw stored pages -> raw job records (snapshot replay, §1)."""
        raise NotImplementedError

    def fetch_job(self, tenant_slug: str, external_job_id: str):
        for raw in self.list_jobs(tenant_slug):
            if self.external_id(raw) == external_job_id:
                return raw
        return None

    def external_id(self, raw_job) -> str:
        raise NotImplementedError

    def normalize(self, raw_job) -> dict:
        raise NotImplementedError

    def healthcheck(self, tenant_slug: str) -> bool:
        try:
            self.poll(tenant_slug)
            return True
        except (AdapterDegradedError, OversizedFeedError, FetchError,
                RefusedHostError, ValueError):
            # Invalid slugs 404 cleanly (NotFoundError is a FetchError); a
            # refused host (e.g. a redirect off the whitelist) or a malformed
            # URL is this source's failure, never the caller's crash, so
            # probe and CLI-enable paths report it as a failed check.
            return False

    # --- shared normalization helpers -------------------------------------

    def _dedupe(self, raw_jobs: list) -> list:
        return dedupe_jobs(raw_jobs, self.external_id,
                           lambda raw: _material_view(self.normalize(raw)))

    def material_fields(self, raw_job) -> dict:
        """The §3 material-field dict (matching domain.discovery
        MATERIAL_FIELDS) from one raw job record."""
        n = self.normalize(raw_job)
        return {
            "title": n.get("title"),
            "seniority": n.get("seniority"),
            "work_authorization_json": None,  # inactive in V0 (§5)
            "language_requirements_json": None,  # inactive in V0 (§5)
            "description_hash": description_hash(n.get("description")),
            "location_json": _json_or_none({
                "locations": n.get("locations") or [],
                "provenance": n.get("location_provenance"),
            }) if n.get("locations") or n.get("location_provenance") else None,
            "remote_policy_json": _json_or_none({
                "mode": n.get("remote_mode"),
                "restriction_countries": n.get("remote_restriction_countries"),
                "provenance": n.get("remote_provenance"),
            }) if n.get("remote_mode") is not None else None,
            "salary_json": _json_or_none({
                "salary": n["salary"],
                "provenance": n.get("salary_provenance"),
            }) if n.get("salary") is not None else None,
            "apply_url": n.get("apply_url"),
        }

    def apply_support(self) -> str:
        return apply_support_for(self.ats_type)


def _material_view(normalized: dict) -> dict:
    """The normalized fields that count as material for collision purposes."""
    return {k: normalized.get(k) for k in (
        "title", "seniority", "description", "locations", "remote_mode",
        "remote_restriction_countries", "salary", "apply_url")}


def _json_or_none(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _string_or_none(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def integral_int(value):
    """Vendor money toward the integer seam: an integral float converts
    deterministically to int (many APIs emit 90000.0); everything else passes
    through unchanged so normalize_salary treats a stated malformed amount as
    malformed (whole payload -> absent, §5), never silently drops it."""
    if isinstance(value, float) and not isinstance(value, bool) and value.is_integer():
        return int(value)
    return value
