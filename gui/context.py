"""State shared by the GUI's handlers (one per server)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from agent.config import Config
from agent.state import StateStore
from gui.auth import Auth


@dataclass
class GuiContext:
    config: Config
    config_path: str | None
    env_file: Path
    workdir: Path
    state: StateStore
    controller: Any                       # gui.control.ServiceController
    auth: Auth
    port: int
    # (config) -> list of agent.setup_autonomous.Check; injectable so tests never touch the network
    preflight: Callable[[Config], list[Any]] | None = None
    webhook_probe: Callable[[Config], dict[str, Any]] | None = None
    log_paths: list[Path] = field(default_factory=list)

    def reload_config(self) -> Config:
        from agent.setup_autonomous import load_env_into

        cfg = Config.load(self.config_path, env=load_env_into(self.env_file))
        self.config = cfg
        self.controller.config = cfg
        return cfg


CTX: web.AppKey[GuiContext] = web.AppKey("gui_context", GuiContext)
# Typed request storage (aiohttp >= 3.12 warns on string keys); a plain string on older versions.
SESSION_KEY: Any = web.RequestKey("gui_session", object) if hasattr(web, "RequestKey") else "gui_session"


def ctx(request: web.Request) -> GuiContext:
    return request.app[CTX]
