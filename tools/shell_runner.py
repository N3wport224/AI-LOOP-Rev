"""Sandboxed subprocess execution: command allowlist, directory confinement, locking and timeouts."""

from __future__ import annotations

import fcntl
import os
import shlex
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from tools.errors import SandboxViolation, ToolError


@dataclass
class ShellResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@contextmanager
def directory_lock(root: Path, timeout: float = 30.0, poll: float = 0.05) -> Iterator[None]:
    """Exclusive advisory lock on ``root`` so only one sandboxed command runs there at a time."""
    lock_path = root / ".sandbox.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ToolError(f"could not lock {root} within {timeout}s") from exc
                time.sleep(poll)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class ShellRunner:
    def __init__(
        self,
        root: str | os.PathLike[str],
        allowlist: Sequence[str],
        timeout: float = 30.0,
        max_output_bytes: int = 64_000,
        lock_timeout: float = 30.0,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowlist = set(allowlist)
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.lock_timeout = lock_timeout

    def _inside(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def _resolve_cwd(self, cwd: str | None) -> Path:
        target = (self.root / cwd).resolve() if cwd else self.root
        if not self._inside(target):
            raise SandboxViolation(f"cwd {cwd!r} escapes sandbox")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _check_arg(self, arg: str, cwd: Path) -> None:
        candidates = [arg]
        if arg.startswith("-") and "=" in arg:
            candidates.append(arg.split("=", 1)[1])
        for value in candidates:
            if value.startswith("~"):
                raise SandboxViolation(f"home-relative path not allowed: {arg!r}")
            looks_like_path = value.startswith(("/", ".")) or "/" in value
            if looks_like_path and "://" not in value:
                resolved = (cwd / value).resolve()
                if not self._inside(resolved):
                    raise SandboxViolation(f"argument {arg!r} escapes sandbox")

    def validate(self, argv: Sequence[str] | str, cwd: str | None = None) -> tuple[list[str], Path]:
        args = shlex.split(argv) if isinstance(argv, str) else list(argv)
        if not args:
            raise SandboxViolation("empty command")
        program = args[0]
        if "/" in program or program not in self.allowlist:
            raise SandboxViolation(f"command {program!r} is not allowlisted")
        workdir = self._resolve_cwd(cwd)
        for arg in args[1:]:
            self._check_arg(arg, workdir)
        return args, workdir

    def run(self, argv: Sequence[str] | str, cwd: str | None = None, timeout: float | None = None) -> ShellResult:
        args, workdir = self.validate(argv, cwd)
        limit = self.timeout if timeout is None else timeout
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with directory_lock(self.root, self.lock_timeout):
            start = time.monotonic()
            proc = subprocess.Popen(  # noqa: S603 - argv validated, never shell=True
                args,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            timed_out = False
            try:
                out, err = proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                out, err = proc.communicate()
            duration = time.monotonic() - start
        cap = self.max_output_bytes
        return ShellResult(
            argv=args,
            returncode=proc.returncode if not timed_out else -9,
            stdout=out[:cap].decode("utf-8", errors="replace"),
            stderr=err[:cap].decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration=duration,
        )
