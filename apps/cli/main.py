"""open-career CLI: init, migrate, export, import.

Instance data lives under $OPEN_CAREER_INSTANCE (default ./instance) and is
never tracked (OC-26).
"""

import argparse
import json
import sys
from pathlib import Path

from adapters.storage.instance import backups_dir, db_path, instance_dir
from adapters.storage.migrations import migrate
from adapters.storage.portability import export_to_file, import_from_file


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="open-career")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the instance directory and run migrations").set_defaults(func=cmd_init)
    sub.add_parser("migrate", help="apply pending migrations (backs up first)").set_defaults(func=cmd_migrate)

    p_export = sub.add_parser("export", help="dump the instance database to JSON")
    p_export.add_argument("file", help="output JSON file")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="load a JSON dump into the instance database")
    p_import.add_argument("file", help="input JSON file")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
