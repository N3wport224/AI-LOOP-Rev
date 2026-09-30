"""Contract between the engine and strategy modules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from tools import Toolkit


@dataclass
class TaskContext:
    tools: "Toolkit"
    hypothesis: dict[str, Any]
    payload: dict[str, Any]
    attempt: int = 1

    @property
    def params(self) -> dict[str, Any]:
        return self.hypothesis["params"]

    @property
    def niche(self) -> str:
        return self.params["niche"]

    @property
    def degraded(self) -> bool:
        """True on retries: strategies should shrink their workload (fewer sources, smaller batches)."""
        return self.attempt > 1


@dataclass
class TaskResult:
    ok: bool
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)
    # Set when the result proves the hypothesis cannot work (e.g. no data exists for the niche),
    # so the engine should pivot now instead of waiting N iterations.
    invalidates_hypothesis: bool = False


class Strategy(ABC):
    name: str = "strategy"
    #: task names this strategy can execute
    tasks: tuple[str, ...] = ()

    @abstractmethod
    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        """Execute ``task``. Raise on failure so the engine can retry with adjusted parameters."""
