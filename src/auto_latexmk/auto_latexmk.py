#!/usr/bin/env python3

import argparse
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TextIO

from pathspec import GitIgnoreSpec
from tqdm import tqdm

from auto_latexmk import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logger.propagate = False

STOP_EVENT = threading.Event()
CACHE_LOCKS: dict[str, threading.Lock] = {}
CACHE_LOCKS_GUARD = threading.Lock()


class AutoLatexmkError(Exception):
    pass


def configure_debug_logging(path):
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    if path is None:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        return

    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


@dataclass(frozen=True)
class CompileTask:
    index: int
    tex_file: str
    subdir: str
    engine: str
    timeout: int
    engine_reason: str | None = None


@dataclass
class CompileResult:
    task: CompileTask
    status: str
    elapsed_time: float
    command: list[str]
    error: str | None = None
    log_file: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "succeeded"

    def to_json(self) -> dict:
        data = {
            "index": self.task.index,
            "path": self.task.tex_file,
            "engine": self.task.engine,
            "status": self.status,
            "elapsed_time": round(self.elapsed_time, 3),
        }
        if self.error:
            data["error"] = self.error
        if self.log_file:
            data["log_file"] = self.log_file
        return data


@dataclass
class RunSummary:
    status: str
    elapsed_time: float
    results: list[CompileResult]
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(result.success for result in self.results)

    @property
    def failed(self) -> int:
        return sum(not result.success for result in self.results)

    def to_json(self) -> dict:
        data = {
            "schema_version": 1,
            "status": self.status,
            "elapsed_time": round(self.elapsed_time, 3),
            "summary": {
                "total": self.total,
                "succeeded": self.succeeded,
                "failed": self.failed,
            },
            "tasks": [
                result.to_json()
                for result in sorted(self.results, key=lambda item: item.task.index)
            ],
        }
        if self.error:
            data["error"] = self.error
        return data


class Reporter(ABC):
    def __init__(
        self,
        *,
        stream: TextIO,
        color: bool = False,
    ):
        self.stream = stream
        self.color = color and self.stream.isatty()

    def _paint(self, text: str, code: str) -> str:
        if not self.color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _print_failure_details(self, summary: RunSummary) -> None:
        for result in sorted(summary.results, key=lambda item: item.task.index):
            if result.success:
                continue
            print(
                f"\n{self._paint('FAILED', '31')}  "
                f"{result.task.tex_file} "
                f"({result.task.engine}) ({result.elapsed_time:.2f}s)",
                file=self.stream,
            )
            if result.error:
                for line in result.error.splitlines():
                    print(f"  {line}", file=self.stream)
            if result.log_file:
                print(f"  Log: {result.log_file}", file=self.stream)

    def _print_dry_run(self, tasks: list[CompileTask]) -> None:
        print(f"{len(tasks)} task(s) planned:", file=self.stream)
        index_width = len(str(max(len(tasks), 1)))
        for task in tasks:
            print(
                f"  [{task.index:>{index_width}}/{len(tasks)}] "
                f"{task.tex_file} ({task.engine})",
                file=self.stream,
            )

    @abstractmethod
    def start(self, tasks: list[CompileTask]) -> None:
        pass

    @abstractmethod
    def task_finished(self, result: CompileResult) -> None:
        pass

    @abstractmethod
    def finish(self, summary: RunSummary) -> None:
        pass

    @abstractmethod
    def dry_run(self, tasks: list[CompileTask]) -> None:
        pass

    @abstractmethod
    def no_compile(self) -> None:
        pass

    @abstractmethod
    def fatal_error(self, message: str) -> None:
        pass

    @abstractmethod
    def interrupted(self) -> None:
        pass

    def close(self) -> None:
        pass


class ProgressBarReporter(Reporter):
    def __init__(
        self,
        *,
        color: bool,
        jobs: int = 1,
        stream: TextIO | None = None,
    ):
        super().__init__(stream=stream or sys.stderr, color=color)
        self.jobs = jobs
        self._bar = None
        self._base_bar_format = ""
        self._succeeded = 0
        self._failed = 0

    def start(self, tasks: list[CompileTask]) -> None:
        self._total = len(tasks)
        index_width = len(str(max(self._total, 1)))
        jobs_suffix = f" {self.jobs} jobs" if self.jobs > 1 else ""
        self._base_bar_format = (
            f"{{desc}} {{n_fmt:>{index_width}}}/{{total_fmt}} "
            "|{bar:20}| {percentage:3.0f}% "
            f"[{{elapsed}}]{jobs_suffix}"
        )
        self._bar = tqdm(
            total=self._total,
            desc="Compile",
            bar_format=self._base_bar_format,
            colour="green" if self.color else None,
            file=self.stream,
        )

    def task_finished(self, result: CompileResult) -> None:
        bar = self._bar
        if bar is None:
            raise RuntimeError("Progress reporter has not been started")

        if result.success:
            self._succeeded += 1
        else:
            self._failed += 1

        status = f"{self._succeeded} OK"
        if self._failed:
            status += f", {self._failed} FAILED"
            if self.color:
                bar.colour = "red"
        bar.bar_format = f"{self._base_bar_format} {status}"
        bar.update(1)
        if not result.success:
            bar.refresh()

    def finish(self, summary: RunSummary) -> None:
        self.close()
        self._print_failure_details(summary)

    def dry_run(self, tasks: list[CompileTask]) -> None:
        self._print_dry_run(tasks)

    def no_compile(self) -> None:
        print("Compilation skipped.", file=self.stream)

    def fatal_error(self, message: str) -> None:
        self.close()
        print(f"ERROR: {message}", file=self.stream)

    def interrupted(self) -> None:
        self.close()
        print("Interrupted.", file=self.stream)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


class ListReporter(Reporter):
    def __init__(
        self,
        *,
        color: bool,
        stream: TextIO | None = None,
    ):
        super().__init__(stream=stream or sys.stderr, color=color)
        self._total = 0
        self._completed = 0
        self._index_width = 1

    def start(self, tasks: list[CompileTask]) -> None:
        self._total = len(tasks)
        self._index_width = len(str(max(self._total, 1)))

    def task_finished(self, result: CompileResult) -> None:
        self._completed += 1
        status = "OK" if result.success else "FAILED"
        label = self._paint(f"{status:<6}", "32" if result.success else "31")
        print(
            f"[{self._completed:>{self._index_width}}/{self._total}] "
            f"{label} {result.task.tex_file} "
            f"({result.task.engine}) ({result.elapsed_time:.2f}s)",
            file=self.stream,
        )

    def finish(self, summary: RunSummary) -> None:
        print(
            f"Compiled {summary.total} file(s) in {summary.elapsed_time:.2f}s: "
            f"{summary.succeeded} succeeded, {summary.failed} failed",
            file=self.stream,
        )
        self._print_failure_details(summary)

    def dry_run(self, tasks: list[CompileTask]) -> None:
        self._print_dry_run(tasks)

    def no_compile(self) -> None:
        print("Compilation skipped.", file=self.stream)

    def fatal_error(self, message: str) -> None:
        print(f"ERROR: {message}", file=self.stream)

    def interrupted(self) -> None:
        print("Interrupted.", file=self.stream)


class JsonReporter(Reporter):
    def __init__(self, *, stream: TextIO | None = None):
        super().__init__(stream=stream or sys.stdout)

    def start(self, tasks: list[CompileTask]) -> None:
        return None

    def task_finished(self, result: CompileResult) -> None:
        return None

    def finish(self, summary: RunSummary) -> None:
        self._write(summary.to_json())

    def dry_run(self, tasks: list[CompileTask]) -> None:
        self._write(
            {
                "schema_version": 1,
                "status": "dry-run",
                "elapsed_time": 0.0,
                "summary": {
                    "total": len(tasks),
                    "succeeded": 0,
                    "failed": 0,
                },
                "tasks": [
                    {
                        "index": task.index,
                        "path": task.tex_file,
                        "engine": task.engine,
                        "status": "planned",
                    }
                    for task in tasks
                ],
            }
        )

    def no_compile(self) -> None:
        self._write(
            {
                "schema_version": 1,
                "status": "succeeded",
                "elapsed_time": 0.0,
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "tasks": [],
            }
        )

    def fatal_error(self, message: str) -> None:
        self._write(
            RunSummary(
                status="failed",
                elapsed_time=0.0,
                results=[],
                error=message,
            ).to_json()
        )

    def interrupted(self) -> None:
        self._write(
            RunSummary(
                status="interrupted",
                elapsed_time=0.0,
                results=[],
                error="Interrupted by user",
            ).to_json()
        )

    def close(self) -> None:
        return None

    def _write(self, data: dict) -> None:
        json.dump(data, self.stream, ensure_ascii=False, indent=2)
        self.stream.write("\n")
        self.stream.flush()


def create_reporter(output_mode: str, *, color: bool, jobs: int = 1) -> Reporter:
    if output_mode == "progressbar":
        return ProgressBarReporter(color=color, jobs=jobs)
    if output_mode == "list":
        return ListReporter(color=color)
    if output_mode == "json":
        return JsonReporter()
    raise ValueError(f"Unknown output mode: {output_mode}")


def _matches_patterns(name, patterns):
    """Return True if *name* matches any fnmatch pattern in *patterns*.

    Patterns are normalized by stripping trailing path separators first.
    Exact names (e.g. ``ref``) and globs (e.g. ``ref*``, ``figures-*``) are both supported.
    """
    for pat in patterns:
        if fnmatch.fnmatch(name, pat.rstrip("/\\")):
            return True
    return False


class CompilerPrefCache:
    """Persistent per-file compiler preference cache.

    Stores which compiler successfully compiled each main .tex file.
    Cache file defaults to ``~/.cache/auto-latexmk/compiler-preferences.json``.
    """

    def __init__(self, cache_path=None):
        if cache_path is None:
            self._path = os.path.join(
                os.path.expanduser("~"),
                ".cache",
                "auto-latexmk",
                "compiler-preferences.json",
            )
        else:
            self._path = cache_path
        self._data: dict = {}
        self._pending: dict = {}
        self._loaded = False

    def _read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read compiler preferences: %s", e)
            return {}

    def _load(self):
        if self._loaded:
            return
        self._data = self._read()
        self._data.update(self._pending)
        self._loaded = True

    def get(self, abs_path: str) -> str | None:
        """Return cached engine preference, or *None* if not found."""
        self._load()
        entry = self._data.get(abs_path)
        if isinstance(entry, dict):
            return entry.get("engine")
        return None

    def set(self, abs_path: str, engine: str):
        """Record a successful compilation with *engine*."""
        entry = {
            "engine": engine,
            "updated_at": time.time(),
        }
        self._pending[abs_path] = entry
        self._data[abs_path] = entry

    def save(self):
        """Merge current updates and atomically write preferences to disk."""
        if not self._pending:
            return

        cache_dir = os.path.dirname(os.path.abspath(self._path))
        temp_path = None
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with _exclusive_file_lock(f"{self._path}.lock"):
                merged = self._read()
                merged.update(self._pending)

                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(self._path)}.",
                    suffix=".tmp",
                    dir=cache_dir,
                )
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self._path)
                temp_path = None

                self._data = merged
                self._pending.clear()
                self._loaded = True
            logger.debug("Compiler preferences saved to %s", self._path)
        except OSError as e:
            logger.warning("Failed to save compiler preferences: %s", e)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass


@contextmanager
def _exclusive_file_lock(lock_path):
    """Hold process-local and cross-process locks for a cache update."""
    normalized_path = os.path.abspath(lock_path)
    with CACHE_LOCKS_GUARD:
        process_lock = CACHE_LOCKS.setdefault(normalized_path, threading.Lock())

    with process_lock, open(normalized_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            if os.path.getsize(normalized_path) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


ACTIVE_PROCESSES = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()


def terminate_active_processes():
    STOP_EVENT.set()
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for process in processes:
        try:
            process.terminate()
        except OSError:
            logger.debug("Failed to terminate process", exc_info=True)
    time.sleep(0.2)
    for process in processes:
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            logger.debug("Failed to kill process", exc_info=True)


def clean_all_aux_subdirs_if_exist(path, exclude_patterns):
    """
    Clean .aux directories.
    If path is a directory, traverse recursively and respect exclude rules.
    """
    if exclude_patterns is None:
        exclude_patterns = []

    path = os.path.abspath(path).replace("\\", "/")

    if os.path.isfile(path) and path.endswith(".tex"):
        aux_dir_path = os.path.join(os.path.dirname(path), ".aux").replace("\\", "/")
        if os.path.exists(aux_dir_path) and os.path.isdir(aux_dir_path):
            try:
                shutil.rmtree(aux_dir_path)
                logger.debug(f"Deleted {aux_dir_path}")
            except OSError as e:
                raise AutoLatexmkError(f"Failed to delete {aux_dir_path}: {e}") from e

    elif os.path.isdir(path):
        for subdir, dirs, _ in os.walk(path, topdown=True):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".") and not _matches_patterns(d, exclude_patterns)
            ]

            aux_dir_path = os.path.join(subdir, ".aux").replace("\\", "/")
            if os.path.exists(aux_dir_path) and os.path.isdir(aux_dir_path):
                try:
                    shutil.rmtree(aux_dir_path)
                    logger.debug(f"Deleted {aux_dir_path}")
                except OSError as e:
                    raise AutoLatexmkError(
                        f"Failed to delete {aux_dir_path}: {e}"
                    ) from e
    else:
        raise AutoLatexmkError(
            f"{path} does not exist or is not a directory/.tex file."
        )


def find_latex_log_file(tex_file, aux_dir):
    jobname = os.path.splitext(os.path.basename(tex_file))[0]
    log_path = os.path.join(aux_dir, f"{jobname}.log").replace("\\", "/")
    return log_path if os.path.exists(log_path) else None


def strip_tex_resource_summary(lines):
    """
    Remove TeX resource usage summary at the end of log.
    """
    for i, line in enumerate(lines):
        if line.startswith("Here is how much of TeX's memory you used"):
            return lines[:i]
    return lines


def extract_error_from_log_file(log_path, *, max_lines, tail_lines):
    """
    Extract the first meaningful LaTeX / latexmk error block from a .log file.
    If nothing is captured, fall back to the last `tail_lines` lines.
    """
    if not log_path or not os.path.exists(log_path):
        return "Failed to open log file", "Unknown"

    ERROR_START_KEYWORDS = [
        "latexmk:",
        "latex error",
        "package error",
        "fatal error",
        "emergency stop",
        "undefined control sequence",
        "missing input file",
        "i can't find",
        "! ",
    ]

    error_lines = []
    capture = False
    all_lines = []

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = line.rstrip("\n")
                all_lines.append(raw)
                lower = raw.lower()

                if not capture:
                    if any(key in lower for key in ERROR_START_KEYWORDS):
                        capture = True
                        error_lines.append(raw)
                    continue

                error_lines.append(raw)

                if (
                    len(error_lines) >= max_lines
                    or "fatal error occurred" in lower
                    or "latexmk: errors" in lower
                ):
                    break

        if error_lines:
            return "\n".join(error_lines), "First error block in the log file"

        # fallback: tail of log
        all_lines = strip_tex_resource_summary(all_lines)
        tail = all_lines[-tail_lines:]

        return "\n".join(tail), "Tail of the log file"

    except OSError as e:
        return f"Failed to parse log file: {e}", "Unknown"


def run_single_compile_task(task: CompileTask) -> CompileResult:
    if STOP_EVENT.is_set():
        return CompileResult(
            task=task,
            status="cancelled",
            elapsed_time=0.0,
            command=[],
            error="Interrupted before start",
        )

    tex_file = task.tex_file
    subdir = task.subdir
    engine = task.engine
    timeout = task.timeout

    engine_map = {
        "pdflatex": "pdf",
        "xelatex": "pdfxe",
        "lualatex": "pdflua",
    }
    mode_flag = engine_map.get(engine, engine)

    out_dir = os.path.abspath(subdir).replace("\\", "/")
    aux_dir = os.path.abspath(os.path.join(subdir, ".aux")).replace("\\", "/")

    latex_full_command = [
        "latexmk",
        "-file-line-error",
        "-halt-on-error",
        "-interaction=nonstopmode",
        "-synctex=1",
        f"-{mode_flag}",
        f"-auxdir={aux_dir}",
        f"-outdir={out_dir}",
        tex_file,
    ]

    logger.debug(f"Compiling {tex_file} ({engine})")
    logger.debug(f"Full command: {' '.join(latex_full_command)}")

    start_time = time.time()
    process = None
    compiler_output = ""
    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            process = subprocess.Popen(
                latex_full_command,
                cwd=subdir,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.add(process)

            deadline = time.time() + timeout if timeout else None
            while True:
                if STOP_EVENT.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                    output.seek(0)
                    compiler_output = output.read().decode(
                        encoding="utf-8", errors="replace"
                    )
                    logger.debug(
                        "Compiler output for %s:\n%s",
                        tex_file,
                        compiler_output.rstrip(),
                    )
                    return CompileResult(
                        task=task,
                        status="cancelled",
                        elapsed_time=time.time() - start_time,
                        command=latex_full_command,
                        error="Interrupted by user",
                    )

                if deadline is not None and time.time() > deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                    output.seek(0)
                    compiler_output = output.read().decode(
                        encoding="utf-8", errors="replace"
                    )
                    logger.error(
                        "Compilation of %s timed out after %ss", tex_file, timeout
                    )
                    logger.debug(
                        "Compiler output for %s:\n%s",
                        tex_file,
                        compiler_output.rstrip(),
                    )
                    return CompileResult(
                        task=task,
                        status="failed",
                        elapsed_time=time.time() - start_time,
                        command=latex_full_command,
                        error=f"Compilation timed out after {timeout}s",
                    )

                returncode = process.poll()
                if returncode is None:
                    time.sleep(0.1)
                    continue
                break

            output.seek(0)
            compiler_output = output.read().decode(encoding="utf-8", errors="replace")

        elapsed_time = time.time() - start_time
        logger.debug("Compiler output for %s:\n%s", tex_file, compiler_output.rstrip())
        logger.debug(
            "Process for %s exited with code %d after %.2fs",
            tex_file,
            returncode,
            elapsed_time,
        )
        if returncode == 0:
            logger.debug("Successfully compiled %s", tex_file)
            return CompileResult(
                task=task,
                status="succeeded",
                elapsed_time=elapsed_time,
                command=latex_full_command,
            )

        logger.error("Failed to compile %s", tex_file)
        log_file = find_latex_log_file(tex_file, aux_dir)
        error = f"Compilation failed with return code {returncode}"
        if log_file:
            error_content, _ = extract_error_from_log_file(
                log_file, max_lines=20, tail_lines=20
            )
            if error_content:
                error = error_content
        elif compiler_output:
            error = "\n".join(compiler_output.splitlines()[-20:])
        return CompileResult(
            task=task,
            status="failed",
            elapsed_time=elapsed_time,
            command=latex_full_command,
            error=error,
            log_file=log_file,
        )

    except Exception as e:
        logger.exception("Error during compilation of %s", tex_file)
        return CompileResult(
            task=task,
            status="failed",
            elapsed_time=time.time() - start_time,
            command=latex_full_command,
            error=str(e),
        )
    finally:
        if process is not None:
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.discard(process)


def run_compile_tasks(tasks, *, jobs: int, reporter):
    task_results = []

    STOP_EVENT.clear()
    interrupted = False

    executor = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = {
            executor.submit(run_single_compile_task, task): task for task in tasks
        }
        for future in as_completed(futures):
            try:
                task_result = future.result()
                task_results.append(task_result)
                reporter.task_finished(task_result)
            except Exception as e:
                task = futures[future]
                logger.exception("Error running task %s", task.tex_file)
                result = CompileResult(
                    task=task,
                    status="failed",
                    elapsed_time=0.0,
                    command=[],
                    error=str(e),
                )
                task_results.append(result)
                reporter.task_finished(result)
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("Interrupted. Stopping running latexmk processes")
        terminate_active_processes()
        raise
    finally:
        executor.shutdown(wait=not interrupted, cancel_futures=interrupted)
        if interrupted:
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.clear()

    return task_results


def strip_latex_comment(line: str) -> str:
    """Remove an unescaped LaTeX comment from a source line."""
    for index, character in enumerate(line):
        if character != "%":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return line[:index]
    return line


def is_main_tex_file(tex_file_path):
    """Return whether a .tex file contains a document class declaration."""
    try:
        with open(tex_file_path, "r", encoding="utf-8") as f:
            return any("\\documentclass" in strip_latex_comment(line) for line in f)

    except (OSError, UnicodeError) as e:
        logger.error(f"Error reading {tex_file_path}: {e}")
        return False


def get_tex_engine(
    tex_file_path, *, default_engine, pref_cache: CompilerPrefCache | None
):
    """
    Determine which LaTeX engine to use for a .tex file.
    - Use *default_engine* immediately when explicitly selected by the CLI
    - Otherwise check the first line for a shebang (% !TEX ...)
    - Then consult *pref_cache* (if given)
    - Finally fall back to xelatex
    Returns (engine, line_info)
    """
    if default_engine is not None:
        return default_engine, "command-line option"

    try:
        with open(tex_file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line.startswith("% !TEX"):
                if "xelatex" in first_line:
                    return "xelatex", first_line
                if "pdflatex" in first_line:
                    return "pdflatex", first_line
                if "lualatex" in first_line:
                    return "lualatex", first_line

    except (OSError, UnicodeError) as e:
        logger.error(f"Error reading {tex_file_path}: {e}")
        return (_resolve_fallback_engine(tex_file_path, pref_cache), None)

    return (_resolve_fallback_engine(tex_file_path, pref_cache), None)


def _resolve_fallback_engine(tex_file_path, pref_cache: CompilerPrefCache | None):
    """Resolve engine when no command-line option or % !TEX directive was found."""
    if pref_cache is not None:
        cached = pref_cache.get(tex_file_path)
        if cached is not None:
            logger.debug(
                "Using compiler preference cache for %s: %s", tex_file_path, cached
            )
            return cached
    return "xelatex"


def generate_compile_tasks_in_dir(
    path,
    *,
    default_engine,
    timeout: int,
    exclude_patterns: list,
    pref_cache: CompilerPrefCache | None,
    use_gitignore: bool = True,
):
    tasks = []
    exclude_patterns = exclude_patterns or []
    base_exclude = [".git", ".aux"]

    logger.debug(f"Dir mode: scanning directory: {path}")

    ignore_scopes = {}

    for subdir, dirs, files in os.walk(path, topdown=True):
        parent_scopes = ignore_scopes.get(os.path.dirname(subdir), [])
        scopes = list(parent_scopes)
        gitignore_path = os.path.join(subdir, ".gitignore")
        if use_gitignore and os.path.isfile(gitignore_path):
            try:
                with open(gitignore_path, "r", encoding="utf-8") as f:
                    scopes.append((subdir, GitIgnoreSpec.from_lines(f)))
            except OSError as e:
                logger.warning(f"Could not read {gitignore_path}: {e}")
        ignore_scopes[subdir] = scopes

        def is_gitignored(candidate, *, is_dir=False, _scopes=tuple(scopes)):
            ignored = False
            for scope_dir, spec in _scopes:
                relative = os.path.relpath(candidate, scope_dir).replace("\\", "/")
                if is_dir:
                    relative += "/"
                result = spec.check_file(relative)
                if result.include is not None:
                    ignored = result.include
            return ignored

        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and not _matches_patterns(d, base_exclude)
            and not _matches_patterns(d, exclude_patterns)
            and not (
                use_gitignore and is_gitignored(os.path.join(subdir, d), is_dir=True)
            )
        ]

        tex_file_list = [
            f
            for f in files
            if f.endswith(".tex")
            and not _matches_patterns(f, exclude_patterns)
            and not (use_gitignore and is_gitignored(os.path.join(subdir, f)))
        ]
        for tex_file_item in tex_file_list:
            tex_file = os.path.abspath(os.path.join(subdir, tex_file_item)).replace(
                "\\", "/"
            )
            if is_main_tex_file(tex_file):
                engine, append_info = get_tex_engine(
                    tex_file, default_engine=default_engine, pref_cache=pref_cache
                )

                tasks.append(
                    CompileTask(
                        index=len(tasks) + 1,
                        tex_file=tex_file,
                        subdir=subdir.replace("\\", "/"),
                        engine=engine,
                        engine_reason=append_info,
                        timeout=timeout,
                    )
                )
    return tasks


def generate_single_task(
    path, *, default_engine, timeout: int, pref_cache: CompilerPrefCache | None
):
    if not path.endswith(".tex"):
        raise AutoLatexmkError(f"{path} is not a .tex file.")

    if not is_main_tex_file(path):
        raise AutoLatexmkError(f"{path} is not a LaTeX main file.")

    engine, append_info = get_tex_engine(
        path, default_engine=default_engine, pref_cache=pref_cache
    )

    task = CompileTask(
        index=1,
        tex_file=path,
        subdir=os.path.dirname(path).replace("\\", "/"),
        engine=engine,
        engine_reason=append_info,
        timeout=timeout,
    )
    return [task]


def generate_tasks_from_input(
    path,
    *,
    default_engine,
    timeout: int,
    exclude_patterns: list,
    pref_cache: CompilerPrefCache | None,
    use_gitignore: bool = True,
):
    path = os.path.abspath(path).replace("\\", "/")

    if os.path.isfile(path):  # single file mode
        return generate_single_task(
            path, default_engine=default_engine, timeout=timeout, pref_cache=pref_cache
        )
    elif os.path.isdir(path):  # dir mode
        return generate_compile_tasks_in_dir(
            path,
            default_engine=default_engine,
            timeout=timeout,
            exclude_patterns=exclude_patterns,
            use_gitignore=use_gitignore,
            pref_cache=pref_cache,
        )
    else:
        raise AutoLatexmkError(f"{path} does not exist.")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="auto-latexmk",
        description="Compile or clean LaTeX (.tex) files in a directory or a single .tex file.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=os.getcwd(),
        type=str,
        help="Directory to recursively search for .tex files (default: pwd) or a single .tex file.",
    )
    parser.add_argument(
        "--output-mode",
        choices=["progressbar", "list", "json"],
        default="progressbar",
        help="Console output mode. (default: progressbar)",
    )
    engine_group = parser.add_mutually_exclusive_group()
    engine_group.add_argument(
        "-pdfxe",
        action="store_const",
        const="xelatex",
        dest="engine",
        help="Force XeLaTeX compiler.",
    )
    engine_group.add_argument(
        "-pdf",
        action="store_const",
        const="pdflatex",
        dest="engine",
        help="Force pdfLaTeX compiler.",
    )
    engine_group.add_argument(
        "-pdflua",
        action="store_const",
        const="lualatex",
        dest="engine",
        help="Force LuaLaTeX compiler.",
    )
    parser.set_defaults(engine=None)

    parser.add_argument(
        "--debug-log",
        type=str,
        metavar="FILE",
        help="Write detailed diagnostic information to a file.",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable colored output"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which .tex tasks would be compiled, without actually running compilation.",
    )
    parser.add_argument(
        "-c",
        "--pre-clean",
        action="store_true",
        help="Clean .aux/ directories before compiling.",
    )
    parser.add_argument(
        "--no-compile",
        action="store_true",
        help="Skip compilation entirely (useful with --pre-clean).",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="Number of concurrent jobs; use 1 for sequential compilation. "
        "(default: min(4, CPU count))",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Per-file compilation timeout in seconds. (default: 180s)",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Exclude a matching directory or .tex file; repeat for multiple fnmatch patterns.",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_false",
        dest="use_gitignore",
        help="Do not exclude files and directories matched by .gitignore during directory scans.",
    )
    parser.set_defaults(use_gitignore=True)

    parser.add_argument(
        "--no-cache-pref",
        action="store_false",
        dest="use_compiler_cache",
        help="Disable compiler preference cache (~/.cache/auto-latexmk/).",
    )
    parser.set_defaults(use_compiler_cache=True)

    parser.add_argument("-v", "--version", action="version", version=__version__)

    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    return args


def main():
    reporter = None
    try:
        args = parse_args()
        default_jobs = min(4, os.cpu_count() or 1)
        jobs = args.jobs or default_jobs
        reporter = create_reporter(
            args.output_mode,
            color=not args.no_color,
            jobs=jobs,
        )
        try:
            configure_debug_logging(args.debug_log)
        except OSError as e:
            reporter.fatal_error(f"Failed to create debug log: {e}")
            return 1

        if args.exclude:
            args.exclude = [d.rstrip("/\\") for d in args.exclude]

        pref_cache = CompilerPrefCache() if args.use_compiler_cache else None

        logger.debug(f"Path: {args.path}")
        logger.debug(f"Default engine: {args.engine}")

        logger.debug(f"Jobs: {jobs}")

        run_start = time.time()
        if args.dry_run:
            tasks = generate_tasks_from_input(
                path=args.path,
                default_engine=args.engine,
                timeout=args.timeout,
                exclude_patterns=args.exclude,
                use_gitignore=args.use_gitignore,
                pref_cache=pref_cache,
            )
            logger.debug("Discovered %d task(s)", len(tasks))
            reporter.dry_run(tasks)
            return 0

        if args.pre_clean:
            clean_all_aux_subdirs_if_exist(args.path, exclude_patterns=args.exclude)
            logger.debug("Pre-clean completed")

        if args.no_compile:
            reporter.no_compile()
            return 0

        tasks = generate_tasks_from_input(
            path=args.path,
            default_engine=args.engine,
            timeout=args.timeout,
            exclude_patterns=args.exclude,
            use_gitignore=args.use_gitignore,
            pref_cache=pref_cache,
        )
        logger.debug("Discovered %d task(s)", len(tasks))
        for task in tasks:
            logger.debug(
                "Task %d: %s; engine=%s; reason=%s",
                task.index,
                task.tex_file,
                task.engine,
                task.engine_reason or "fallback",
            )

        reporter.start(tasks)
        tasks_results = run_compile_tasks(tasks, jobs=jobs, reporter=reporter)

        if pref_cache is not None:
            for result in tasks_results:
                if result.success:
                    pref_cache.set(
                        result.task.tex_file,
                        result.task.engine,
                    )
            pref_cache.save()

        failed = any(not result.success for result in tasks_results)
        summary = RunSummary(
            status="failed" if failed else "succeeded",
            elapsed_time=time.time() - run_start,
            results=tasks_results,
        )
        reporter.finish(summary)
        return 1 if failed else 0
    except AutoLatexmkError as e:
        logger.error("%s", e)
        if reporter is not None:
            reporter.fatal_error(str(e))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        terminate_active_processes()
        if reporter is not None:
            reporter.interrupted()
        return 130
    except Exception as e:
        logger.exception("Unexpected error")
        if reporter is not None:
            reporter.fatal_error(str(e))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        if reporter is not None:
            reporter.close()
        configure_debug_logging(None)


if __name__ == "__main__":
    raise SystemExit(main())
