"""Local-filesystem StorageAdapter, rooted at the instance directory."""

from pathlib import Path

from domain.ports import StorageAdapter


class LocalStorageAdapter(StorageAdapter):
    def __init__(self, root: Path):
        self._root = root.resolve()

    def _resolve(self, relative_path: str) -> Path:
        path = (self._root / relative_path).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"path escapes the instance root: {relative_path}")
        return path

    def read_text(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text()

    def write_text(self, relative_path: str, content: str) -> None:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def write_bytes(self, relative_path: str, content: bytes) -> None:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).exists()
