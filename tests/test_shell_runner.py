import threading
import time

import pytest

from tools.errors import SandboxViolation, ToolError
from tools.shell_runner import ShellRunner, directory_lock


@pytest.fixture
def runner(tmp_path):
    return ShellRunner(tmp_path / "ws", ["echo", "python3", "ls", "cat", "sleep"], timeout=5)


def test_runs_allowlisted_command(runner):
    res = runner.run(["echo", "hello"])
    assert res.ok and res.stdout.strip() == "hello"


def test_string_command_is_split_not_shelled(runner):
    res = runner.run("echo 'a; rm -rf /'")
    assert res.stdout.strip() == "a; rm -rf /"


def test_rejects_non_allowlisted(runner):
    with pytest.raises(SandboxViolation):
        runner.run(["rm", "-rf", "x"])
    with pytest.raises(SandboxViolation):
        runner.run(["/bin/echo", "x"])


@pytest.mark.parametrize("arg", ["/etc/passwd", "../secret", "--file=/etc/shadow", "~/x", "a/../../b"])
def test_rejects_path_escape_in_args(runner, arg):
    with pytest.raises(SandboxViolation):
        runner.run(["cat", arg])


def test_allows_paths_inside_sandbox_and_urls(runner):
    (runner.root / "sub").mkdir()
    (runner.root / "sub" / "f.txt").write_text("data")
    assert runner.run(["cat", "sub/f.txt"]).stdout == "data"
    runner.validate(["echo", "https://example.com/a/b"])


def test_cwd_confined(runner):
    with pytest.raises(SandboxViolation):
        runner.run(["ls"], cwd="../")
    res = runner.run(["python3", "-c", "import os; print(os.getcwd())"], cwd="inner")
    assert res.stdout.strip().endswith("/ws/inner")


def test_timeout_kills_process(runner):
    start = time.monotonic()
    res = runner.run(["sleep", "10"], timeout=0.3)
    assert res.timed_out and not res.ok
    assert time.monotonic() - start < 5


def test_env_is_minimal(runner, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    res = runner.run(["python3", "-c", "import os; print(os.environ.get('SECRET_TOKEN'))"])
    assert res.stdout.strip() == "None"


def test_output_truncated(tmp_path):
    r = ShellRunner(tmp_path / "ws", ["python3"], max_output_bytes=100)
    res = r.run(["python3", "-c", "print('x'*10000)"])
    assert len(res.stdout) == 100


def test_directory_lock_is_exclusive(tmp_path):
    held = threading.Event()
    release = threading.Event()

    def holder():
        with directory_lock(tmp_path):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    with pytest.raises(ToolError):
        with directory_lock(tmp_path, timeout=0.2):
            pass
    release.set()
    t.join()
    with directory_lock(tmp_path, timeout=1):
        pass
