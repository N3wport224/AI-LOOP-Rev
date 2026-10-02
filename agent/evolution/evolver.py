"""Sandboxed self-modification: shadow worktree → full checks → fast-forward merge.

**What may change** (``scope_violation``). A patch is refused before anything runs when any
file in it is outside the evolvable scope:

* allowed: ``strategies/``, ``tools/syndication/``, ``tools/page_builder.py``, ``seeds/``;
* never (the immutable core): ``agent/supervisor.py``, ``tools/storefront/webhook_listener.py``,
  ``agent/recovery.py``, the tests (``tests/``, ``test_*.py``, ``conftest.py``) and this package
  (``agent/evolution/``), so the agent can't loosen its own gate. Everything not listed as
  allowed is refused too.

**How much may change** (``content_violation``). Inside an allowed Python file, only an
*evolvable block* may change: the lines between ``# <evolved:NAME>`` and ``# </evolved:NAME>``,
which must stay one assignment of a pure literal (``ast.literal_eval``): a table of patterns,
aliases or copy. Everything outside the blocks must be byte-identical. ``seeds/*.json`` must
stay valid JSON objects. Code paths, imports and control flow are therefore never rewritten:
the agent tunes its heuristics' data, and the code that uses the data is reviewed by a human.

**Shadow-branch workflow** (``Evolver.attempt``):

1. refuse if the checkout has uncommitted changes to tracked files, or HEAD is detached;
2. ``git worktree add -b auto/evolution-<timestamp>`` at HEAD, in a temporary directory;
3. apply the patch there (refused if a target changed since the diagnosis: stale patch);
4. lint (syntax, pyflakes, ruff when installed; JSON validity), then ``pytest -v -W error``
   and ``automonetize test-full-loop``, all inside the worktree, with secrets stripped from
   the environment;
5. anything fails → remove the worktree and branch, record the output in
   ``data/evolution_log.db``, blacklist the patch, cooldown (24 h);
6. everything passes → commit in the worktree as the agent
   (``[Auto-Evolution] <title>``), check the checkout is still clean and at the same HEAD,
   ``git merge --ff-only`` into the active branch, remove the worktree. Nothing is pushed.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent.evolution.log import EvolutionLog, utcnow

ALLOWED_PREFIXES = ("strategies/", "tools/syndication/", "seeds/")
ALLOWED_FILES = ("tools/page_builder.py",)
FORBIDDEN_FILES = ("agent/supervisor.py", "tools/storefront/webhook_listener.py", "agent/recovery.py")
FORBIDDEN_PREFIXES = ("tests/", "agent/evolution/", ".git/", ".github/")
AGENT_NAME = "AutoMonetize Agent"
AGENT_EMAIL = "agent@automonetize.invalid"
MAX_BLOCK_CHARS = 50_000
MAX_SEED_BYTES = 64_000
REGION_RE = re.compile(r"^# <evolved:(?P<name>[A-Za-z_]\w*)>[^\n]*\n(?P<body>.*?)^# </evolved:(?P=name)>[ \t]*$", re.M | re.S)


class BoundaryViolation(Exception):
    """The patch touches something evolution may not change."""


def sha256(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


@dataclass
class FileChange:
    path: str                  # repo-relative, POSIX
    before: str | None         # content the patch was computed against (None = new file)
    after: str

    @property
    def before_sha(self) -> str | None:
        return sha256(self.before)


@dataclass
class EvolutionHypothesis:
    kind: str                  # parser_heuristics | intent_vocabulary | copy_variants
    title: str                 # commit subject after "[Auto-Evolution] "
    rationale: str
    metric: str
    baseline: float
    expected: float
    changes: list[FileChange] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for ch in sorted(self.changes, key=lambda c: c.path):
            h.update(f"{ch.path}\0{sha256(ch.after)}\0".encode())
        return h.hexdigest()[:32]

    def diff(self) -> str:
        out = []
        for ch in self.changes:
            out.extend(difflib.unified_diff((ch.before or "").splitlines(keepends=True), ch.after.splitlines(keepends=True),
                                            f"a/{ch.path}" if ch.before is not None else "/dev/null", f"b/{ch.path}"))
        return "".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "rationale": self.rationale, "metric": self.metric,
                "baseline": self.baseline, "expected": self.expected, "files": [c.path for c in self.changes],
                "fingerprint": self.fingerprint, "evidence": self.evidence}


# ----------------------------------------------------------------------------- boundaries
def scope_violation(path: str) -> str | None:
    """Why ``path`` may not be evolved, or None if it may."""
    if not path or path.startswith(("/", "~")) or "\\" in path or "\0" in path:
        return f"{path!r}: absolute or malformed path"
    norm = posixpath.normpath(path)
    if norm != path or norm == ".." or norm.startswith("../"):
        return f"{path!r}: path must be normalised and inside the repository"
    name = posixpath.basename(norm)
    if norm in FORBIDDEN_FILES or norm.startswith(FORBIDDEN_PREFIXES) or name.startswith("test_") or name == "conftest.py":
        return f"{path}: immutable core (never evolved)"
    if any(part.startswith(".") for part in norm.split("/")):
        return f"{path}: hidden path"
    if not (norm in ALLOWED_FILES or norm.startswith(ALLOWED_PREFIXES)):
        return f"{path}: outside the evolvable scope ({', '.join(ALLOWED_PREFIXES + ALLOWED_FILES)})"
    if norm.startswith("seeds/"):
        return None if norm.endswith(".json") else f"{path}: seeds/ holds JSON data only"
    return None if norm.endswith(".py") else f"{path}: only Python modules and seeds/*.json can evolve"


def regions(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m in REGION_RE.finditer(text):
        if m.group("name") in found:
            raise BoundaryViolation(f"evolvable block {m.group('name')} is declared twice")
        found[m.group("name")] = m.group("body")
    return found


def _mask(text: str) -> str:
    def blank(m: re.Match[str]) -> str:
        whole, start = m.group(0), m.start()
        return whole[: m.start("body") - start] + "\0\n" + whole[m.end("body") - start:]

    return REGION_RE.sub(blank, text)


def _literal_assignment(body: str, where: str) -> tuple[str, str]:
    """(target name, annotation dump) of a block that must be one literal assignment."""
    try:
        tree = ast.parse(body)
    except SyntaxError as exc:
        raise BoundaryViolation(f"{where}: not valid Python ({exc.msg}, line {exc.lineno})") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Assign, ast.AnnAssign)):
        raise BoundaryViolation(f"{where}: an evolvable block must be exactly one assignment")
    node = tree.body[0]
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name) or node.value is None:
        raise BoundaryViolation(f"{where}: an evolvable block assigns one name")
    try:
        ast.literal_eval(node.value)
    except (ValueError, TypeError, SyntaxError, RecursionError) as exc:
        raise BoundaryViolation(f"{where}: the value must be a pure literal (data, not code)") from exc
    annotation = ast.dump(node.annotation) if isinstance(node, ast.AnnAssign) else ""
    return targets[0].id, annotation


def rewrite_region(text: str, name: str, body: str) -> str:
    """``text`` with the evolvable block ``name`` replaced by ``body`` (one assignment)."""
    m = next((m for m in REGION_RE.finditer(text) if m.group("name") == name), None)
    if m is None:
        raise BoundaryViolation(f"no evolvable block {name}")
    body = body if body.endswith("\n") else body + "\n"
    return text[: m.start("body")] + body + text[m.end("body"):]


def content_violation(change: FileChange) -> str | None:
    """Why the new content isn't an acceptable evolution of the old, or None."""
    try:
        if change.path.endswith(".json"):
            if len(change.after.encode()) > MAX_SEED_BYTES:
                return f"{change.path}: seed file over {MAX_SEED_BYTES} bytes"
            if not isinstance(json.loads(change.after), dict):
                return f"{change.path}: a seed file must hold a JSON object"
            return None
        if change.before is None:
            return f"{change.path}: evolution can't create Python modules, only change evolvable blocks"
        old, new = regions(change.before), regions(change.after)
        if not old:
            return f"{change.path}: has no evolvable blocks (# <evolved:NAME> … # </evolved:NAME>)"
        if set(old) != set(new):
            return f"{change.path}: evolvable blocks can't be added, removed or renamed"
        if _mask(change.before) != _mask(change.after):
            return f"{change.path}: changes outside the evolvable blocks"
        changed = [n for n in new if new[n] != old[n]]
        if not changed:
            return f"{change.path}: no change"
        for n in changed:
            if len(new[n]) > MAX_BLOCK_CHARS:
                return f"{change.path}:{n}: block over {MAX_BLOCK_CHARS} characters"
            if _literal_assignment(new[n], f"{change.path}:{n}") != _literal_assignment(old[n], f"{change.path}:{n} (before)"):
                return f"{change.path}:{n}: the assigned name and its type annotation must not change"
        compile(change.after, change.path, "exec")
    except BoundaryViolation as exc:
        return str(exc)
    except (ValueError, SyntaxError) as exc:
        return f"{change.path}: {exc}"
    return None


def patch_violations(hyp: EvolutionHypothesis) -> list[str]:
    if not hyp.changes:
        return ["the hypothesis carries no patch"]
    problems = [p for ch in hyp.changes if (p := scope_violation(ch.path))]
    if problems:
        return problems  # boundary first: never even look at forbidden content
    if len({ch.path for ch in hyp.changes}) != len(hyp.changes):
        return ["a file appears twice in the patch"]
    return [p for ch in hyp.changes if (p := content_violation(ch))]


# ----------------------------------------------------------------------------- checks
@dataclass
class Check:
    name: str
    argv: list[str]
    timeout: float = 1200.0


def default_checks(config: Any) -> list[Check]:
    timeout = float(getattr(config, "evolution_check_timeout_seconds", 1200))
    checks = [Check("pytest -v -W error", [sys.executable, "-m", "pytest", "-v", "-W", "error", "-p", "no:cacheprovider"], timeout)]
    if getattr(config, "evolution_run_simulator", True):
        checks.append(Check("automonetize test-full-loop", [sys.executable, "-m", "cli.test_loop", "--no-color"], min(timeout, 300.0)))
    return checks


# the notification addresses work like passwords too: anyone holding one can post as you
_SECRETISH = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|CHAT_WEBHOOK_URL|NTFY_TOPIC|HEALTHCHECK_URL", re.I)


def sandbox_env(worktree: Path) -> dict[str, str]:
    """The environment for checks: no secrets, no agent overrides, the worktree first on the path."""
    from agent.evolution.hot_reload import LISTEN_FD_ENV

    # GIT_CONFIG_COUNT/KEY_n/VALUE_n go as a family (dropping only the KEY_n ones breaks git), and
    # their values can carry credentials (http.extraHeader), so none of them reach the checks.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AUTOMONETIZE_", "GIT_CONFIG_")) and not _SECRETISH.search(k)
           and k not in ("ENABLE_AUTONOMOUS_CODE_EVOLUTION", "DRY_RUN", "PUBLIC_WEBHOOK_URL", LISTEN_FD_ENV, "PYTHONPATH")}
    env["PYTHONPATH"] = str(worktree)
    env["AM_NO_SELF_UPDATE"] = "1"  # checks run the agent; it must never update the real checkout
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@dataclass
class Outcome:
    status: str                 # rejected | aborted | failed | merged
    attempt_id: int | None = None
    detail: str = ""
    commit_sha: str | None = None
    branch: str = ""
    output: str = ""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class Evolver:
    def __init__(self, repo: str | Path, log: EvolutionLog, config: Any, run: Runner = subprocess.run,
                 checks: list[Check] | None = None, clock: Callable[[], datetime] = utcnow):
        self.repo = Path(repo).resolve()
        self.log = log
        self.config = config
        self.run = run
        self.checks = default_checks(config) if checks is None else checks
        self.clock = clock

    # -- git ----------------------------------------------------------------------------------
    def git(self, *args: str, cwd: Path | None = None, check: bool = True, timeout: float = 120) -> str:
        proc = self.run(["git", "-c", f"user.name={AGENT_NAME}", "-c", f"user.email={AGENT_EMAIL}", "-c", "commit.gpgsign=false",
                         *args], cwd=str(cwd or self.repo), capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
        return (proc.stdout or "").strip()

    def dirty(self) -> str:
        return self.git("status", "--porcelain", "--untracked-files=no")

    def current_branch(self) -> str | None:
        name = self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return name or None

    def read(self, path: str) -> str | None:
        p = self.repo / path
        return p.read_text() if p.is_file() else None

    # -- the workflow ---------------------------------------------------------------------------
    def attempt(self, hyp: EvolutionHypothesis) -> Outcome:
        problems = patch_violations(hyp)
        if problems:
            aid = self.log.start(hyp)
            detail = "rejected before running anything: " + "; ".join(problems)
            self.log.finish(aid, "rejected", detail)
            self.log.blacklist(hyp.fingerprint, detail, aid)
            return Outcome("rejected", aid, detail)
        branch = self.current_branch()
        dirty = self.dirty()
        if branch is None or dirty:
            aid = self.log.start(hyp)
            detail = "detached HEAD" if branch is None else f"uncommitted changes in the checkout: {dirty[:300]}"
            self.log.finish(aid, "aborted", f"not evolving: {detail} (the agent never commits over your work)")
            return Outcome("aborted", aid, detail)

        base = self.git("rev-parse", "HEAD")
        name = f"auto/evolution-{self.clock().strftime('%Y%m%d-%H%M%S')}"
        worktree = Path(tempfile.mkdtemp(prefix="automonetize-evolution-"))
        aid = self.log.start(hyp, name, base)
        output: list[str] = []
        merged = False
        try:
            self.git("worktree", "add", "-b", name, str(worktree), base, timeout=300)
            self._apply(worktree, hyp)
            for label, ok, out in self._verify(worktree, hyp):
                output.append(f"===== {label}: {'ok' if ok else 'FAILED'} =====\n{out[-8000:]}")
                if not ok:
                    return self._fail(aid, hyp, name, f"{label} failed", output)
            self.git("add", "--", *[ch.path for ch in hyp.changes], cwd=worktree)
            self.git("commit", "-q", "-m", self.commit_message(hyp, aid), cwd=worktree, timeout=300)
            # The checkout may have moved while the checks ran (a human commit, a pull).
            if self.dirty() or self.git("rev-parse", "HEAD") != base or self.current_branch() != branch:
                self.log.finish(aid, "aborted", "the checkout changed while the checks ran; will re-diagnose", "\n".join(output))
                return Outcome("aborted", aid, "checkout moved", branch=name)
            self.git("merge", "--ff-only", "-q", name, timeout=300)
            merged = True
            sha = self.git("rev-parse", "HEAD")
            detail = f"merged into {branch} at {sha[:12]} (fast-forward from {base[:12]})"
            self.log.finish(aid, "merged", detail, "\n".join(output), commit_sha=sha)
            return Outcome("merged", aid, detail, sha, name, "\n".join(output))
        except BoundaryViolation as exc:
            self.log.finish(aid, "rejected", f"rejected: {exc}", "\n".join(output))
            self.log.blacklist(hyp.fingerprint, str(exc), aid)
            return Outcome("rejected", aid, str(exc), branch=name)
        except Exception as exc:  # noqa: BLE001 - any surprise is a failed attempt, never a crash
            output.append(traceback.format_exc())
            return self._fail(aid, hyp, name, f"{type(exc).__name__}: {exc}", output)
        finally:
            self._cleanup(worktree, name, merged)

    def _apply(self, worktree: Path, hyp: EvolutionHypothesis) -> None:
        root = worktree.resolve()
        for ch in hyp.changes:
            target = worktree / ch.path
            if target.is_symlink() or not target.resolve().is_relative_to(root):
                raise BoundaryViolation(f"{ch.path}: resolves outside the worktree")
            current = target.read_text() if target.is_file() else None
            if sha256(current) != ch.before_sha:
                raise BoundaryViolation(f"{ch.path}: changed since the diagnosis (stale patch)")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(ch.after)

    def _verify(self, worktree: Path, hyp: EvolutionHypothesis):
        """Yields (label, ok, output). Stops at the first failure (the caller returns)."""
        yield self._lint(worktree, hyp)
        env = sandbox_env(worktree)
        for check in self.checks:
            try:
                proc = self.run(check.argv, cwd=str(worktree), env=env, capture_output=True, text=True, timeout=check.timeout)
                yield check.name, proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired as exc:
                yield check.name, False, f"timed out after {check.timeout:.0f}s\n{exc.stdout or ''}"

    def _lint(self, worktree: Path, hyp: EvolutionHypothesis) -> tuple[str, bool, str]:
        notes = []
        py = [ch.path for ch in hyp.changes if ch.path.endswith(".py")]
        for ch in hyp.changes:
            text = (worktree / ch.path).read_text()
            try:
                if ch.path.endswith(".json"):
                    json.loads(text)
                else:
                    compile(text, ch.path, "exec")
            except (ValueError, SyntaxError) as exc:
                return "lint", False, f"{ch.path}: {exc}"
        env = sandbox_env(worktree)
        tools = []
        if py and importlib.util.find_spec("pyflakes") is not None:
            tools.append(("pyflakes", [sys.executable, "-m", "pyflakes", *py]))
        if py and shutil.which("ruff"):
            tools.append(("ruff", ["ruff", "check", "--no-cache", *py]))
        for label, argv in tools:
            proc = self.run(argv, cwd=str(worktree), env=env, capture_output=True, text=True, timeout=120)
            notes.append(f"{label}: {'clean' if proc.returncode == 0 else 'FAILED'}\n{proc.stdout}{proc.stderr}")
            if proc.returncode != 0:
                return "lint", False, "\n".join(notes)
        return "lint", True, "\n".join(notes) or "syntax ok"

    def _fail(self, aid: int, hyp: EvolutionHypothesis, branch: str, why: str, output: list[str]) -> Outcome:
        until = self.log.enter_cooldown(float(getattr(self.config, "evolution_cooldown_hours", 24)), f"attempt #{aid} failed: {why}")
        self.log.finish(aid, "failed", f"{why}; worktree discarded; cooling down until {until.isoformat(timespec='minutes')}",
                        "\n".join(output))
        self.log.blacklist(hyp.fingerprint, why, aid)
        return Outcome("failed", aid, why, branch=branch, output="\n".join(output))

    def _cleanup(self, worktree: Path, branch: str, merged: bool) -> None:
        self.git("worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
        self.git("worktree", "prune", check=False)
        self.git("branch", "-d" if merged else "-D", branch, check=False)

    @staticmethod
    def commit_message(hyp: EvolutionHypothesis, attempt_id: int) -> str:
        return (f"[Auto-Evolution] {hyp.title}\n\n{hyp.rationale}\n\n"
                f"Metric: {hyp.metric}: {hyp.baseline:g} -> expected {hyp.expected:g}\n"
                f"Evolution-Attempt: {attempt_id}\nEvolution-Fingerprint: {hyp.fingerprint}\n")
