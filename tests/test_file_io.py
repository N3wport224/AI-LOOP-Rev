import pytest

from tools.errors import SandboxViolation, ToolError
from tools.file_io import SandboxedFileIO


def test_roundtrip_text_json_csv(tmp_path):
    fio = SandboxedFileIO(tmp_path / "box")
    fio.write_text("a/b.txt", "hello")
    assert fio.read_text("a/b.txt") == "hello"
    fio.write_json("x.json", {"k": [1, 2]})
    assert fio.read_json("x.json") == {"k": [1, 2]}
    fio.write_csv("rows.csv", [{"a": 1, "b": ["x", "y"], "c": None}], ["a", "b", "c"])
    assert fio.read_csv("rows.csv") == [{"a": "1", "b": "x; y", "c": ""}]
    assert fio.list_dir("a") == ["b.txt"]
    assert fio.delete("a/b.txt") and not fio.exists("a/b.txt")


@pytest.mark.parametrize("bad", ["../escape.txt", "/etc/passwd", "a/../../x"])
def test_rejects_escape(tmp_path, bad):
    fio = SandboxedFileIO(tmp_path / "box")
    with pytest.raises(SandboxViolation):
        fio.write_text(bad, "x")


def test_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    fio = SandboxedFileIO(tmp_path / "box")
    (fio.root / "link").symlink_to(outside)
    with pytest.raises(SandboxViolation):
        fio.write_text("link/pwn.txt", "x")


def test_size_limit(tmp_path):
    fio = SandboxedFileIO(tmp_path / "box", max_bytes=10)
    with pytest.raises(ToolError):
        fio.write_text("big.txt", "x" * 11)


def test_cannot_delete_root(tmp_path):
    fio = SandboxedFileIO(tmp_path / "box")
    with pytest.raises(SandboxViolation):
        fio.delete(".")
