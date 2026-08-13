"""Curated-layer loader (OC-37 §2): merge the reviewed Italy/EU-remote tenant
file into the registry with origin 'curated'. Curated rows are never
auto-pruned; a tenant already harvested is upgraded to curated. Run manually.

File format: a YAML list of flat mappings, restricted to plain scalars (the
subset below is parsed in-repo, no dependency; comments and blank lines ok):

    # italy-eu-remote.yaml
    - ats_type: greenhouse
      tenant_slug: acme
      company_name: Acme S.r.l.
      industry: fintech
      company_stage: series_b
      company_size_band: 51-200

Required keys: ats_type, tenant_slug. Optional: company_name, policy_notes,
and the reviewed metadata fields industry / company_stage / company_size_band
(landed with origin 'curated'). --dry-run prints what it would do.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.storage.instance import db_path  # noqa: E402
from adapters.storage.sqlite_discovery import SqliteSourceRegistryRepository  # noqa: E402
from domain.discovery import Source  # noqa: E402
from domain.ids import new_id  # noqa: E402

ATS_TYPES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")
ALLOWED_KEYS = frozenset({
    "ats_type", "tenant_slug", "company_name", "policy_notes",
    "industry", "company_stage", "company_size_band",
})
METADATA_FIELDS = ("industry", "company_stage", "company_size_band")


class CuratedFileError(ValueError):
    pass


def parse_curated_yaml(text: str) -> list[dict]:
    """Parse the restricted YAML subset: a list of flat string mappings.
    Anything outside the subset is a hard error, never a guess."""
    entries: list[dict] = []
    current: dict | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip() if not _in_quotes(raw_line) \
            else raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {}
            entries.append(current)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        elif current is None:
            raise CuratedFileError(
                f"line {line_number}: expected a list item ('- key: value')")
        if ":" not in stripped:
            raise CuratedFileError(f"line {line_number}: expected 'key: value'")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key not in ALLOWED_KEYS:
            raise CuratedFileError(
                f"line {line_number}: unknown key '{key}'"
                f" (allowed: {sorted(ALLOWED_KEYS)})")
        if key in current:
            raise CuratedFileError(f"line {line_number}: duplicate key '{key}'")
        current[key] = value
    for i, entry in enumerate(entries):
        if entry.get("ats_type") not in ATS_TYPES:
            raise CuratedFileError(
                f"entry {i + 1}: 'ats_type' must be one of {list(ATS_TYPES)}")
        if not entry.get("tenant_slug"):
            raise CuratedFileError(f"entry {i + 1}: 'tenant_slug' is required")
    return entries


def _in_quotes(line: str) -> bool:
    """Comment stripping guard: a '#' inside a quoted value is content."""
    head = line.split("#", 1)[0]
    return head.count('"') % 2 == 1 or head.count("'") % 2 == 1


def merge_curated(conn, entries: list[dict], dry_run: bool) -> tuple[int, int]:
    registry = SqliteSourceRegistryRepository(conn)
    existing = {(s.ats_type, s.tenant_slug): s for s in registry.list_all()}
    added = updated = 0
    for entry in entries:
        key = (entry["ats_type"], entry["tenant_slug"])
        label = f"[{key[0]}] {key[1]}"
        if key in existing:
            source_id = existing[key].id
            if dry_run:
                print(f"would upgrade {label} to origin curated")
            else:
                with conn:  # curated is never auto-pruned; origin upgrades
                    conn.execute(
                        "UPDATE sources SET origin = 'curated',"
                        " company_name = COALESCE(?, company_name),"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                        " WHERE id = ?",
                        (entry.get("company_name"), source_id))
            updated += 1
        else:
            if dry_run:
                print(f"would add {label} (origin: curated)")
            else:
                source = Source(
                    id=new_id("src"), ats_type=entry["ats_type"],
                    tenant_slug=entry["tenant_slug"], origin="curated",
                    company_name=entry.get("company_name"),
                    policy_notes=entry.get("policy_notes"))
                registry.add(source)
                source_id = source.id
            added += 1
        if not dry_run:
            for field in METADATA_FIELDS:
                if entry.get(field):
                    registry.set_reviewed_metadata(
                        source_id, field, entry[field], "curated")
        elif any(entry.get(field) for field in METADATA_FIELDS):
            print(f"  with reviewed metadata: "
                  + ", ".join(f"{f}={entry[f]}" for f in METADATA_FIELDS
                              if entry.get(f)))
    return added, updated


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="curated tenants YAML file")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would change; write nothing")
    args = parser.parse_args(argv)
    entries = parse_curated_yaml(Path(args.file).read_text())
    path = db_path()
    if not path.exists():
        print("instance not initialized (run: open-career init)", file=sys.stderr)
        raise SystemExit(1)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        added, updated = merge_curated(conn, entries, args.dry_run)
    finally:
        conn.close()
    verb = "would add" if args.dry_run else "added"
    print(f"{verb} {added} curated sources, {updated} upgraded/refreshed")


if __name__ == "__main__":
    main()
