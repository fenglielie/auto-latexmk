from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskType(str, Enum):
    COMPILE = "compile"
    CLEAN = "clean"
    REBUILD = "rebuild"


class StepType(str, Enum):
    CLEAN = "clean"
    COMPILE = "compile"


@dataclass(frozen=True)
class CommandSpec:
    step: StepType
    cwd: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class LatexTask:
    index: int
    task_type: TaskType
    tex_file: str
    engine: str | None
    engine_reason: str | None
    commands: tuple[CommandSpec, ...]


@dataclass(frozen=True)
class TaskPlan:
    root_dir: str
    task_type: TaskType
    tasks: tuple[LatexTask, ...]


@dataclass
class StepResult:
    command: CommandSpec
    status: str
    elapsed_time: float
    exit_code: int | None = None
    error_message: str | None = None
    error_excerpt: str | None = None
    log_file: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "succeeded"


@dataclass
class TaskResult:
    task: LatexTask
    status: str
    elapsed_time: float
    steps: list[StepResult]
    output_file: str | None = None
    log_error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "succeeded"

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"


@dataclass
class RunSummary:
    elapsed_time: float
    results: list[TaskResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(result.success for result in self.results)

    @property
    def cancelled(self) -> int:
        return sum(result.cancelled for result in self.results)

    @property
    def failed(self) -> int:
        return self.total - self.succeeded - self.cancelled
