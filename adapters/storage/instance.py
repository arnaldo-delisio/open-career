"""Instance directory layout (OC-26: all personal data lives here, never tracked)."""

import os
from pathlib import Path

DB_FILENAME = "open-career.sqlite3"


def instance_dir() -> Path:
    """The instance data directory: $OPEN_CAREER_INSTANCE or ./instance."""
    return Path(os.environ.get("OPEN_CAREER_INSTANCE", "instance"))


def db_path(instance: Path | None = None) -> Path:
    return (instance or instance_dir()) / DB_FILENAME


def backups_dir(instance: Path | None = None) -> Path:
    return (instance or instance_dir()) / "backups"
