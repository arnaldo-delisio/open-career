"""open-career CLI: init, migrate, onboard, profile, edges, export, import.

Instance data lives under $OPEN_CAREER_INSTANCE (default ./instance) and is
never tracked (OC-26).
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from adapters.models.claude_code import ClaudeCodeAdapter, ModelCallError
from adapters.storage.instance import backups_dir, db_path, instance_dir
from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.portability import export_to_file, import_from_file
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.onboarding import CvReadError, run_onboarding
from domain.ports import ModelUnavailableError
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteCareerGoalRepository,
    SqliteExperienceRepository,
)
from domain.profile import InvalidProfileValueError, UnknownProfileFieldError


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        print("instance not initialized (run: open-career init)", file=sys.stderr)
        raise SystemExit(1)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_init(_args: argparse.Namespace) -> None:
    instance = instance_dir()
    instance.mkdir(parents=True, exist_ok=True)
    backups_dir(instance).mkdir(parents=True, exist_ok=True)
    applied = migrate(db_path(instance))
    print(f"initialized {instance} (applied migrations: {applied or 'none, up to date'})")


def cmd_migrate(_args: argparse.Namespace) -> None:
    applied = migrate(db_path())
    print(f"applied migrations: {applied or 'none, up to date'}")


def cmd_export(args: argparse.Namespace) -> None:
    out = Path(args.file)
    try:
        export_to_file(db_path(), out)
    except ValueError as e:
        print(f"export failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"exported to {out}")


def cmd_import(args: argparse.Namespace) -> None:
    src = Path(args.file)
    try:
        import_from_file(db_path(), src)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"import failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"imported from {src}")


def cmd_onboard(args: argparse.Namespace) -> None:
    cv_path = Path(args.cv) if args.cv else None
    if cv_path is not None and not cv_path.exists():
        print(f"CV file not found: {cv_path}", file=sys.stderr)
        raise SystemExit(1)
    conn = _connect()
    storage = LocalStorageAdapter(instance_dir())
    try:
        model = ClaudeCodeAdapter() if cv_path is not None else None
        try:
            run_onboarding(conn, storage, model, cv_path)
        except (ModelCallError, ModelUnavailableError, CvReadError) as e:
            # A failed or unavailable model is not a dead backend: inform, then
            # degrade to the same interview questions the no-CV path runs.
            # (ModelUnavailableError's own message names the missing claude CLI
            # and how to get it.)
            print(f"CV extraction failed: {e}", file=sys.stderr)
            print("Continuing without the CV; you can re-run onboarding later.")
            run_onboarding(conn, storage, None, None)
    except ValueError as e:
        print(f"onboarding failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        conn.close()


def cmd_profile_set(args: argparse.Namespace) -> None:
    conn = _connect()
    try:
        SqliteUserProfileRepository(conn).set_field(args.field, args.value, source="user_edit")
    except (UnknownProfileFieldError, InvalidProfileValueError) as e:
        print(f"profile set failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        conn.close()
    print(f"profile.{args.field} set")


def _print_profile(conn) -> None:
    fields = SqliteUserProfileRepository(conn).get_fields()
    print("Profile:")
    if not fields:
        print("  (empty)")
    for field in sorted(fields):
        print(f"  {field}: {fields[field]}")


def cmd_profile_show(_args: argparse.Namespace) -> None:
    conn = _connect()
    try:
        _print_profile(conn)
    finally:
        conn.close()


def cmd_show(_args: argparse.Namespace) -> None:
    conn = _connect()
    try:
        _print_profile(conn)

        facts = SqliteCareerFactRepository(conn).list_all()
        approved = [f for f in facts if f.user_approved and f.status == "active"]
        by_experience: dict[str | None, list] = {}
        for fact in approved:
            by_experience.setdefault(fact.experience_id, []).append(fact)

        print("\nExperiences:")
        experiences = SqliteExperienceRepository(conn).list_all()
        if not experiences:
            print("  (none)")
        for exp in experiences:
            print(f"  [{exp.kind}] {exp.title} @ {exp.org}"
                  f" ({exp.start_date or '?'} - {exp.end_date or 'present'})")
            for fact in by_experience.get(exp.id, []):
                print(f"    - {fact.statement}")
        unattached = by_experience.get(None, [])
        if unattached:
            print("  (no experience)")
            for fact in unattached:
                print(f"    - {fact.statement}")

        print("\nCapabilities:")
        capabilities = SqliteCapabilityRepository(conn).list_all()
        if not capabilities:
            print("  (none)")
        for cap in capabilities:
            print(f"  {cap.name} ({cap.strength})")

        print("\nGoals:")
        goals = [g for g in SqliteCareerGoalRepository(conn).list_all() if g.status == "active"]
        if not goals:
            print("  (none)")
        for goal in goals:
            print(f"  [{goal.horizon}] {goal.statement}")

        edges_repo = SqliteCareerEdgeRepository(conn)
        edges = edges_repo.list_all()
        active = [e for e in edges if e.superseded_at is None]
        untyped = edges_repo.list_untyped()
        print(f"\nEdges: {len(edges)} total, {len(active)} active,"
              f" {len(untyped)} untyped (see: open-career edges list --untyped)")
    finally:
        conn.close()


def cmd_edges_list(args: argparse.Namespace) -> None:
    conn = _connect()
    try:
        repo = SqliteCareerEdgeRepository(conn)
        edges = repo.list_untyped() if args.untyped else repo.list_all()
    finally:
        conn.close()
    if args.untyped and not edges:
        print("no untyped edges (nothing awaiting re-typing)")
        return
    for e in edges:
        print(f"{e.id}  {e.source_type}:{e.source_id} -{e.edge_type}-> "
              f"{e.target_type}:{e.target_id}  claim={e.claim_kind}"
              f" created_by={e.created_by} verified={e.user_verified}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="open-career")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the instance directory and run migrations").set_defaults(func=cmd_init)
    sub.add_parser("migrate", help="apply pending migrations (backs up first)").set_defaults(func=cmd_migrate)

    p_onboard = sub.add_parser(
        "onboard", help="CV-first onboarding: extraction drafts, you confirm; then gap questions")
    p_onboard.add_argument("cv", nargs="?", default=None, help="optional CV text file")
    p_onboard.set_defaults(func=cmd_onboard)

    sub.add_parser("show", help="print the stored career state, human-readable").set_defaults(func=cmd_show)

    p_profile = sub.add_parser("profile", help="canonical profile fields (closed set, audited writes)")
    profile_sub = p_profile.add_subparsers(dest="profile_command", required=True)
    p_profile_set = profile_sub.add_parser("set", help="set one canonical field")
    p_profile_set.add_argument("field")
    p_profile_set.add_argument("value")
    p_profile_set.set_defaults(func=cmd_profile_set)
    profile_sub.add_parser("show", help="print the profile fields").set_defaults(func=cmd_profile_show)

    p_edges = sub.add_parser("edges", help="career graph edges")
    edges_sub = p_edges.add_subparsers(dest="edges_command", required=True)
    p_edges_list = edges_sub.add_parser("list", help="list edges")
    p_edges_list.add_argument("--untyped", action="store_true",
                              help="only migrated edges with 'unknown' endpoint types")
    p_edges_list.set_defaults(func=cmd_edges_list)

    p_export = sub.add_parser("export", help="dump the instance database to JSON")
    p_export.add_argument("file", help="output JSON file")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="load a JSON dump into the instance database")
    p_import.add_argument("file", help="input JSON file")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except sqlite3.Error as e:
        # One line, never a traceback. Only schema-shaped failures get the
        # invalid-instance wording; a locked db or I/O error is not one.
        detail = str(e)
        schema_shaped = isinstance(e, sqlite3.OperationalError) and any(
            marker in detail
            for marker in ("no such table", "already exists", "file is not a database"))
        if schema_shaped:
            print(f"this does not look like a valid open-career instance ({detail})", file=sys.stderr)
        else:
            print(f"database operation failed: {detail}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
