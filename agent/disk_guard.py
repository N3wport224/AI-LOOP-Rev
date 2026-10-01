"""Disk guard: notice a filling disk before SQLite or a delivery fails.

``check_disk`` (every cycle, a single ``statvfs``) looks at the free space where the data lives:

* below ``disk_warn_gb`` (2 GB): one alert a day, with the biggest folders the agent owns;
* below ``disk_critical_gb`` (0.5 GB): housekeeping runs at once (old logs and unsold dataset
  versions go) and new dataset builds pause (kv ``disk_low``) until there's room again, so the
  databases and purchased files are never the thing that fails.

Shown in ``automonetize doctor``. State: kv ``disk`` = {free_gb, total_gb, at}.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "disk"
LOW = "disk_low"
GB = 1024 ** 3


def folder_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def biggest(data_dir: Path, limit: int = 3) -> list[tuple[str, float]]:
    sizes = [(p.name, folder_size(p) / GB) for p in data_dir.iterdir() if p.is_dir()] if data_dir.exists() else []
    return sorted(sizes, key=lambda x: -x[1])[:limit]


class DiskGuard(Strategy):
    name = "disk_guard"
    tasks = ("check_disk",)

    def __init__(self, usage: Any = shutil.disk_usage):
        self._usage = usage

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        cfg, state = ctx.tools.config, ctx.tools.state
        data = Path(cfg.data_dir)
        usage = self._usage(str(data))
        free = usage.free / GB
        state.set(KEY, {"free_gb": round(free, 2), "total_gb": round(usage.total / GB, 1), "at": state.now()})
        critical = free < float(cfg.disk_critical_gb)
        if critical and not state.get(LOW):
            state.set(LOW, True)
            from agent.housekeeping import Housekeeping

            state.set("housekeeping", {})  # run now, whatever the schedule
            Housekeeping().run("housekeeping", ctx)
        elif not critical and state.get(LOW):
            state.set(LOW, False)
            state.log_action(int(state.get("iteration", 0)), None, "disk", "ok", f"{free:.1f} GB free again; builds resume")
        if free < float(cfg.disk_warn_gb):
            last = state.get("disk_alert_at")
            if not last or state.clock() - datetime.fromisoformat(last) >= timedelta(hours=24):
                top = ", ".join(f"{n} {g:.2f} GB" for n, g in biggest(data)) or "nothing big in the data folder"
                state.log_error("disk_guard", f"Only {free:.1f} GB free on the Mac's disk"
                                              + (" (critical: dataset builds paused)" if critical else "")
                                              + f". Largest agent folders: {top}. Free up space (e.g. empty the Trash, "
                                              "delete large downloads).", kind="alert")
                state.set("disk_alert_at", state.now())
        return TaskResult(True, f"disk: {free:.1f} GB free" + (" (critical)" if critical else ""), {"free_gb": round(free, 2)})


def builds_paused(state: Any) -> bool:
    return bool(state.get(LOW))
