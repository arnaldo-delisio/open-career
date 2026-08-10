import pytest

from adapters.storage.local import LocalStorageAdapter


def test_read_write_roundtrip(tmp_path):
    storage = LocalStorageAdapter(tmp_path)
    storage.write_text("notes/hello.txt", "hi")
    assert storage.exists("notes/hello.txt")
    assert storage.read_text("notes/hello.txt") == "hi"


def test_path_escape_is_rejected(tmp_path):
    storage = LocalStorageAdapter(tmp_path)
    with pytest.raises(ValueError):
        storage.read_text("../outside.txt")
