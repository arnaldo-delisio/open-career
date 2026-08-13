"""Common Crawl CDX harvest bootstrap (OC-37 §2): the one-time, re-runnable
registry seed. Run manually, never from the polling worker.

Queries the CDX index for the five ATS URL patterns, regex-extracts tenant
slugs, dedupes across snapshots, and inserts registry candidate rows (origin
'harvest'; enabled only after a §2 probe). No slug brute-forcing: harvest over
enumeration, locked.

Usage:
    uv run python scripts/harvest_sources.py --index CC-MAIN-2026-26 [--dry-run]
    uv run python scripts/harvest_sources.py --from-file urls.txt [--dry-run]

--from-file processes one URL per line offline (e.g. a saved CDX result).
--dry-run prints what it would insert and writes nothing.
"""

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.storage.instance import db_path  # noqa: E402
from adapters.storage.sqlite_discovery import SqliteSourceRegistryRepository  # noqa: E402
from domain.discovery import Source  # noqa: E402
from domain.ids import new_id  # noqa: E402

CDX_HOST = "https://index.commoncrawl.org"

# The five URL patterns (§2), each with its slug-extraction regex. Slugs are
# path segment one; query strings, fragments, and deeper paths are ignored.
URL_PATTERNS: dict[str, tuple[str, re.Pattern]] = {
    "greenhouse": ("boards.greenhouse.io/*", re.compile(
        r"^https?://(?:www\.)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)(?:[/?#]|$)")),
    "lever": ("jobs.lever.co/*", re.compile(
        r"^https?://(?:www\.)?jobs\.lever\.co/([A-Za-z0-9_-]+)(?:[/?#]|$)")),
    "ashby": ("jobs.ashbyhq.com/*", re.compile(
        r"^https?://(?:www\.)?jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)(?:[/?#]|$)")),
    "workable": ("apply.workable.com/*", re.compile(
        r"^https?://(?:www\.)?apply\.workable\.com/([A-Za-z0-9_-]+)(?:[/?#]|$)")),
    "smartrecruiters": ("careers.smartrecruiters.com/*", re.compile(
        r"^https?://(?:www\.)?careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)(?:[/?#]|$)")),
}

# Path segments that are vendor surface, not tenants.
NON_TENANT_SEGMENTS = frozenset({
    "embed", "api", "v1", "js", "css", "assets", "static", "favicon.ico",
    "robots.txt", "sitemap.xml",
})


def extract_slugs(urls, ats_type: str) -> set[str]:
    """Regex-extract tenant slugs for one ATS; dedupe across snapshots is the
    set itself (URLs recur across many crawls)."""
    _, pattern = URL_PATTERNS[ats_type]
    slugs = set()
    for url in urls:
        match = pattern.match(url.strip())
        if not match:
            continue
        slug = match.group(1)
        if slug.lower() in NON_TENANT_SEGMENTS:
            continue
        slugs.add(slug.lower())
    return slugs


def cdx_urls(index: str, url_pattern: str, page_limit: int | None):
    """Stream matching URLs from the CDX API, collapsed on urlkey so each URL
    appears once per index."""
    base = (f"{CDX_HOST}/{index}-index?url={urllib.parse.quote(url_pattern)}"
            "&output=json&fl=url&collapse=urlkey")
    with urllib.request.urlopen(f"{base}&showNumPages=true", timeout=60) as response:
        pages = json.loads(response.read()).get("pages", 1)
    if page_limit is not None:
        pages = min(pages, page_limit)
    for page in range(pages):
        with urllib.request.urlopen(f"{base}&page={page}", timeout=120) as response:
            for line in response.read().decode().splitlines():
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)["url"]
                except (json.JSONDecodeError, KeyError):
                    continue


def insert_candidates(conn, slugs_by_ats: dict[str, set[str]],
                      dry_run: bool) -> tuple[int, int]:
    registry = SqliteSourceRegistryRepository(conn)
    existing = {(s.ats_type, s.tenant_slug) for s in registry.list_all()}
    inserted = skipped = 0
    for ats_type in sorted(slugs_by_ats):
        for slug in sorted(slugs_by_ats[ats_type]):
            if (ats_type, slug) in existing:
                skipped += 1
                continue
            if dry_run:
                print(f"would insert [{ats_type}] {slug} (origin: harvest)")
                inserted += 1
                continue
            registry.add(Source(id=new_id("src"), ats_type=ats_type,
                                tenant_slug=slug, origin="harvest"))
            inserted += 1
    return inserted, skipped


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=None,
                        help="Common Crawl index name, e.g. CC-MAIN-2026-26")
    parser.add_argument("--from-file", default=None,
                        help="offline mode: file with one URL per line")
    parser.add_argument("--ats", choices=sorted(URL_PATTERNS), default=None,
                        help="restrict to one ATS (default: all five)")
    parser.add_argument("--page-limit", type=int, default=None,
                        help="max CDX pages per pattern (testing/partials)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be inserted; write nothing")
    args = parser.parse_args(argv)
    if bool(args.index) == bool(args.from_file):
        parser.error("exactly one of --index or --from-file is required")

    ats_types = [args.ats] if args.ats else sorted(URL_PATTERNS)
    slugs_by_ats: dict[str, set[str]] = {}
    if args.from_file:
        urls = Path(args.from_file).read_text().splitlines()
        for ats_type in ats_types:
            slugs_by_ats[ats_type] = extract_slugs(urls, ats_type)
    else:
        for ats_type in ats_types:
            pattern, _ = URL_PATTERNS[ats_type]
            print(f"querying {args.index} for {pattern} ...", file=sys.stderr)
            slugs_by_ats[ats_type] = extract_slugs(
                cdx_urls(args.index, pattern, args.page_limit), ats_type)

    path = db_path()
    if not path.exists():
        print("instance not initialized (run: open-career init)", file=sys.stderr)
        raise SystemExit(1)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        inserted, skipped = insert_candidates(conn, slugs_by_ats, args.dry_run)
    finally:
        conn.close()
    verb = "would insert" if args.dry_run else "inserted"
    print(f"{verb} {inserted} candidate sources ({skipped} already registered)")


if __name__ == "__main__":
    main()
