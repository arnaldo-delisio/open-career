"""The five public-API source adapters (OC-37 §1, OC-1). One factory so the
worker and CLI build the full set consistently."""

from adapters.sources.ashby import AshbyAdapter
from adapters.sources.greenhouse import GreenhouseAdapter
from adapters.sources.http import HttpFetcher
from adapters.sources.lever import LeverAdapter
from adapters.sources.smartrecruiters import SmartRecruitersAdapter
from adapters.sources.workable import WorkableAdapter


def build_adapters(fetcher: HttpFetcher | None = None,
                   max_pages_per_poll: int | None = None) -> dict:
    """ats_type -> adapter, all sharing one fetcher (one per-host interval
    ledger across the run)."""
    fetcher = fetcher or HttpFetcher()
    kwargs = {}
    if max_pages_per_poll is not None:
        kwargs["max_pages_per_poll"] = max_pages_per_poll
    return {adapter.ats_type: adapter for adapter in (
        GreenhouseAdapter(fetcher, **kwargs),
        LeverAdapter(fetcher, **kwargs),
        AshbyAdapter(fetcher, **kwargs),
        WorkableAdapter(fetcher, **kwargs),
        SmartRecruitersAdapter(fetcher, **kwargs),
    )}
