"""File access confined to a single root directory."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from tools.errors import SandboxViolation, ToolError


class SandboxedFileIO:
    def __init__(self, root: str | os.PathLike[str], max_bytes: int = 10 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def resolve(self, relpath: str | os.PathLike[str]) -> Path:
        path = Path(relpath)
        target = (path if path.is_absolute() else self.root / path).resolve()
        if target != self.root and self.root not in target.parents:
            raise SandboxViolation(f"path {str(relpath)!r} escapes sandbox {self.root}")
        return target

    # -- raw ----------------------------------------------------------------
    def write_bytes(self, relpath: str, data: bytes) -> Path:
        if len(data) > self.max_bytes:
            raise ToolError(f"refusing to write {len(data)} bytes (limit {self.max_bytes})")
        target = self.resolve(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)  # atomic: readers never see a half-written file
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return target

    def read_bytes(self, relpath: str) -> bytes:
        target = self.resolve(relpath)
        if target.stat().st_size > self.max_bytes:
            raise ToolError(f"{relpath} exceeds read limit of {self.max_bytes} bytes")
        return target.read_bytes()

    # -- text / json / csv ---------------------------------------------------
    def write_text(self, relpath: str, text: str) -> Path:
        return self.write_bytes(relpath, text.encode("utf-8"))

    def read_text(self, relpath: str) -> str:
        return self.read_bytes(relpath).decode("utf-8")

    def write_json(self, relpath: str, obj: Any) -> Path:
        return self.write_text(relpath, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")

    def read_json(self, relpath: str) -> Any:
        return json.loads(self.read_text(relpath))

    def write_csv(self, relpath: str, rows: Iterable[dict[str, Any]], fieldnames: Sequence[str]) -> Path:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(row.get(k)) for k in fieldnames})
        return self.write_text(relpath, buf.getvalue())

    def read_csv(self, relpath: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(self.read_text(relpath))))

    # -- misc ---------------------------------------------------------------
    def exists(self, relpath: str) -> bool:
        return self.resolve(relpath).exists()

    def list_dir(self, relpath: str = ".") -> list[str]:
        target = self.resolve(relpath)
        return sorted(p.name for p in target.iterdir()) if target.is_dir() else []

    def delete(self, relpath: str) -> bool:
        target = self.resolve(relpath)
        if target == self.root:
            raise SandboxViolation("refusing to delete sandbox root")
        if target.is_file():
            target.unlink()
            return True
        return False


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return "" if value is None else value
