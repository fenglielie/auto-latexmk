from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskType(str, Enum):
    COMPILE = "compile"
    CLEAN = "clean"
    CLEAN_COMPILE = "clean-compile"


class Action(str, Enum):
    RUN = "run"
    PREVIEW = "preview"
    EXPORT = "export"


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
    timed_out: bool = False
    error_kind: str | None = None
    error_message: str | None = None
    error_excerpt: str | None = None
    log_file: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "succeeded"

    def to_json(self) -> dict:
        data = {
            "step": self.command.step.value,
            "status": self.status,
            "cwd": self.command.cwd,
            "argv": list(self.command.argv),
            "elapsed_time": round(self.elapsed_time, 3),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }
        if self.error_kind or self.error_message or self.error_excerpt:
            error = {
                "kind": self.error_kind or "exception",
                "message": self.error_message or "Task step failed",
            }
            if self.error_excerpt:
                error["excerpt"] = self.error_excerpt
            if self.log_file:
                error["log_file"] = self.log_file
            data["error"] = error
        return data


@dataclass
class TaskResult:
    task: LatexTask
    status: str
    elapsed_time: float
    steps: list[StepResult]

    @property
    def success(self) -> bool:
        return self.status == "succeeded"

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    def to_json(self) -> dict:
        return {
            "index": self.task.index,
            "path": self.task.tex_file,
            "task_type": self.task.task_type.value,
            "engine": self.task.engine,
            "engine_reason": self.task.engine_reason,
            "status": self.status,
            "elapsed_time": round(self.elapsed_time, 3),
            "steps": [step.to_json() for step in self.steps],
        }


@dataclass
class RunSummary:
    task_type: TaskType
    status: str
    elapsed_time: float
    results: list[TaskResult]
    error_kind: str | None = None
    error_message: str | None = None

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

    def to_json(self) -> dict:
        data = {
            "schema_version": 2,
            "task_type": self.task_type.value,
            "action": Action.RUN.value,
            "status": self.status,
            "elapsed_time": round(self.elapsed_time, 3),
            "summary": {
                "total": self.total,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "cancelled": self.cancelled,
            },
            "tasks": [
                result.to_json()
                for result in sorted(self.results, key=lambda item: item.task.index)
            ],
        }
        if self.error_message:
            data["error"] = {
                "kind": self.error_kind or "setup",
                "message": self.error_message,
            }
        return data
