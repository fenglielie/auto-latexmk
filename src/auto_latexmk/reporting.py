import json
import sys
import threading
from abc import ABC, abstractmethod
from typing import TextIO

from tqdm import tqdm

from auto_latexmk.models import RunSummary, TaskResult, TaskType


class Reporter(ABC):
    def __init__(self, *, task_type: TaskType, stream: TextIO, color=False):
        self.task_type = task_type
        self.stream = stream
        self.color = color and stream.isatty()

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _failure_details(self, summary: RunSummary):
        for result in sorted(summary.results, key=lambda item: item.task.index):
            if result.success:
                continue
            print(
                f"\n{self._paint('FAILED', '31')}  {result.task.tex_file} "
                f"({result.task.task_type.value}) ({result.elapsed_time:.2f}s)",
                file=self.stream,
            )
            for step in result.steps:
                if step.status not in {"failed", "cancelled"}:
                    continue
                print(f"  {step.command.step.value}: {step.error_message}", file=self.stream)
                if step.error_excerpt:
                    for line in step.error_excerpt.splitlines():
                        print(f"    {line}", file=self.stream)
                if step.log_file:
                    print(f"    Log: {step.log_file}", file=self.stream)

    @abstractmethod
    def start(self, tasks): ...

    @abstractmethod
    def task_finished(self, result: TaskResult): ...

    @abstractmethod
    def finish(self, summary: RunSummary): ...

    @abstractmethod
    def fatal_error(self, message: str): ...

    @abstractmethod
    def interrupted(self): ...

    def running_changed(self, running: int):
        return None

    def close(self):
        return None


class ProgressBarReporter(Reporter):
    def __init__(self, *, task_type: TaskType, color: bool, stream=None):
        super().__init__(
            task_type=task_type, stream=stream or sys.stderr, color=color
        )
        self._bar = None
        self._lock = threading.RLock()
        self._total = 0
        self._succeeded = 0
        self._failed = 0
        self._running = 0
        self._width = 1

    def _format(self):
        left = max(self._total - self._succeeded - self._failed, 0)
        suffix = (
            f" {left:>{self._width}} LEFT"
            f" {self._succeeded:>{self._width}} OK"
            f" {self._failed:>{self._width}} FAILED"
        )
        if self._running > 1:
            suffix += f" ({self._running} parallel jobs)"
        return "{desc} |{bar:20}| {percentage:3.0f}% [{elapsed}<{remaining}]" + suffix

    def start(self, tasks):
        with self._lock:
            self._total = len(tasks)
            self._width = len(str(max(self._total, 1)))
            self._bar = tqdm(
                total=self._total,
                desc=self.task_type.value.capitalize(),
                bar_format=self._format(),
                colour="green" if self.color else None,
                file=self.stream,
            )

    def running_changed(self, running: int):
        with self._lock:
            self._running = running
            if self._bar is not None:
                self._bar.bar_format = self._format()
                self._bar.refresh()

    def task_finished(self, result: TaskResult):
        with self._lock:
            if self._bar is None:
                raise RuntimeError("Progress reporter has not been started")
            if result.success:
                self._succeeded += 1
            else:
                self._failed += 1
                if self.color:
                    self._bar.colour = "red"
            self._bar.bar_format = self._format()
            self._bar.update(1)

    def finish(self, summary: RunSummary):
        self.close()
        self._failure_details(summary)

    def fatal_error(self, message: str):
        self.close()
        print(f"ERROR: {message}", file=self.stream)

    def interrupted(self):
        self.close()
        print("Interrupted.", file=self.stream)

    def close(self):
        with self._lock:
            if self._bar is not None:
                self._bar.close()
                self._bar = None


class ListReporter(Reporter):
    def __init__(self, *, task_type: TaskType, color: bool, stream=None):
        super().__init__(
            task_type=task_type, stream=stream or sys.stdout, color=color
        )
        self._total = 0
        self._completed = 0
        self._width = 1

    def start(self, tasks):
        self._total = len(tasks)
        self._width = len(str(max(self._total, 1)))

    def task_finished(self, result: TaskResult):
        self._completed += 1
        status = "OK" if result.success else "FAILED"
        label = self._paint(f"{status:<6}", "32" if result.success else "31")
        detail = result.task.engine or result.task.task_type.value
        print(
            f"[{self._completed:>{self._width}}/{self._total}] {label} "
            f"{result.task.tex_file} ({detail}) ({result.elapsed_time:.2f}s)",
            file=self.stream,
        )

    def finish(self, summary: RunSummary):
        print(
            f"Processed {summary.total} task(s) in {summary.elapsed_time:.2f}s: "
            f"{summary.succeeded} succeeded, {summary.failed} failed, "
            f"{summary.cancelled} cancelled",
            file=self.stream,
        )
        self._failure_details(summary)

    def fatal_error(self, message: str):
        print(f"ERROR: {message}", file=sys.stderr)

    def interrupted(self):
        print("Interrupted.", file=sys.stderr)


class JsonReporter(Reporter):
    def __init__(self, *, task_type: TaskType, stream=None):
        super().__init__(task_type=task_type, stream=stream or sys.stdout)

    def start(self, tasks):
        return None

    def task_finished(self, result: TaskResult):
        return None

    def finish(self, summary: RunSummary):
        self._write(summary.to_json())

    def fatal_error(self, message: str):
        self._write(
            RunSummary(
                task_type=self.task_type,
                status="failed",
                elapsed_time=0.0,
                results=[],
                error_kind="setup",
                error_message=message,
            ).to_json()
        )

    def interrupted(self):
        self._write(
            RunSummary(
                task_type=self.task_type,
                status="interrupted",
                elapsed_time=0.0,
                results=[],
                error_kind="cancelled",
                error_message="Interrupted by user",
            ).to_json()
        )

    def _write(self, data):
        json.dump(data, self.stream, ensure_ascii=False, indent=2)
        self.stream.write("\n")
        self.stream.flush()


def create_reporter(output_mode: str, *, task_type: TaskType, color: bool):
    if output_mode == "progressbar":
        return ProgressBarReporter(task_type=task_type, color=color)
    if output_mode == "list":
        return ListReporter(task_type=task_type, color=color)
    if output_mode == "json":
        return JsonReporter(task_type=task_type)
    raise ValueError(f"Unknown output mode: {output_mode}")
