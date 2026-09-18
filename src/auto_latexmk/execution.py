import os
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from auto_latexmk.models import LatexTask, StepResult, StepType, TaskResult
from auto_latexmk.reporting import Reporter
from auto_latexmk.runtime import AutoLatexmkError, logger

STOP_EVENT = threading.Event()
ACTIVE_PROCESSES = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()


class TaskExecutionInterrupted(Exception):
    def __init__(self, results: list[TaskResult]):
        super().__init__("Task execution interrupted")
        self.results = results


def _signal_process_tree(process, *, force: bool, allow_exited=False) -> bool:
    if not allow_exited and process.poll() is not None:
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        logger.debug("Failed to signal process tree", exc_info=True)
        return False
    return True


def terminate_active_processes():
    STOP_EVENT.set()
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES)
    signalled = [
        process
        for process in processes
        if _signal_process_tree(process, force=False)
    ]
    time.sleep(0.2)
    for process in signalled:
        _signal_process_tree(process, force=True, allow_exited=os.name != "nt")


def _find_log_file(task: LatexTask) -> str | None:
    jobname = os.path.splitext(os.path.basename(task.tex_file))[0]
    path = os.path.join(os.path.dirname(task.tex_file), ".aux", f"{jobname}.log")
    return path.replace("\\", "/") if os.path.exists(path) else None


def _extract_error(log_path: str, *, max_lines=20, tail_lines=20) -> str:
    keywords = (
        "latexmk:",
        "latex error",
        "package error",
        "fatal error",
        "emergency stop",
        "undefined control sequence",
        "missing input file",
        "i can't find",
        "! ",
    )
    captured = []
    all_lines = []
    capture = False
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as log_file:
            for line in log_file:
                raw = line.rstrip("\n")
                all_lines.append(raw)
                lower = raw.lower()
                if not capture and any(keyword in lower for keyword in keywords):
                    capture = True
                if capture:
                    captured.append(raw)
                    if len(captured) >= max_lines:
                        break
        if captured:
            return "\n".join(captured)
        for index, line in enumerate(all_lines):
            if line.startswith("Here is how much of TeX's memory you used"):
                all_lines = all_lines[:index]
                break
        return "\n".join(all_lines[-tail_lines:])
    except OSError as error:
        return f"Failed to parse log file: {error}"


def _cancel_process(process):
    signalled = _signal_process_tree(process, force=False)
    if not signalled:
        return
    parent_still_running = False
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        parent_still_running = True

    if os.name != "nt" or parent_still_running:
        _signal_process_tree(process, force=True, allow_exited=os.name != "nt")
    if parent_still_running:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            logger.debug("Process tree did not exit after forced termination")


def _start_process(command, output):
    if os.name == "nt":
        return subprocess.Popen(
            command.argv,
            cwd=command.cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        command.argv,
        cwd=command.cwd,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def run_step(task: LatexTask, command, *, timeout: int) -> StepResult:
    if STOP_EVENT.is_set():
        return StepResult(
            command=command,
            status="cancelled",
            elapsed_time=0.0,
            error_kind="cancelled",
            error_message="Interrupted before start",
        )

    start_time = time.monotonic()
    process = None
    compiler_output = ""
    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            process = _start_process(command, output)
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.add(process)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if STOP_EVENT.is_set():
                    _cancel_process(process)
                    return StepResult(
                        command=command,
                        status="cancelled",
                        elapsed_time=time.monotonic() - start_time,
                        exit_code=process.returncode,
                        error_kind="cancelled",
                        error_message="Interrupted by user",
                    )
                if time.monotonic() > deadline:
                    _cancel_process(process)
                    return StepResult(
                        command=command,
                        status="failed",
                        elapsed_time=time.monotonic() - start_time,
                        exit_code=process.returncode,
                        timed_out=True,
                        error_kind="timeout",
                        error_message=f"Command timed out after {timeout}s",
                    )
                time.sleep(0.1)

            output.seek(0)
            compiler_output = output.read().decode("utf-8", errors="replace")

        elapsed = time.monotonic() - start_time
        logger.debug(
            "Command in %s: %s\n%s",
            command.cwd,
            " ".join(command.argv),
            compiler_output.rstrip(),
        )
        if process.returncode == 0:
            return StepResult(
                command=command,
                status="succeeded",
                elapsed_time=elapsed,
                exit_code=0,
            )

        log_file = _find_log_file(task) if command.step is StepType.COMPILE else None
        excerpt = (
            _extract_error(log_file)
            if log_file
            else "\n".join(compiler_output.splitlines()[-20:])
        )
        return StepResult(
            command=command,
            status="failed",
            elapsed_time=elapsed,
            exit_code=process.returncode,
            error_kind="latex-log" if log_file else "process-output",
            error_message=f"{command.step.value} failed with exit code {process.returncode}",
            error_excerpt=excerpt or None,
            log_file=log_file,
        )
    except Exception as error:  # noqa: BLE001 - task failures become structured results
        logger.exception("Error running %s for %s", command.step.value, task.tex_file)
        return StepResult(
            command=command,
            status="failed",
            elapsed_time=time.monotonic() - start_time,
            error_kind="exception",
            error_message=str(error),
        )
    finally:
        if process is not None:
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.discard(process)


def run_task(task: LatexTask, *, timeout: int) -> TaskResult:
    start_time = time.monotonic()
    results = []
    failed = False
    cancelled = False
    for command in task.commands:
        if failed or cancelled:
            results.append(
                StepResult(command=command, status="skipped", elapsed_time=0.0)
            )
            continue
        result = run_step(task, command, timeout=timeout)
        results.append(result)
        failed = result.status == "failed"
        cancelled = result.status == "cancelled"

    status = "cancelled" if cancelled else "failed" if failed else "succeeded"
    return TaskResult(
        task=task,
        status=status,
        elapsed_time=time.monotonic() - start_time,
        steps=results,
    )


def run_tasks(tasks, *, jobs: int, timeout: int, reporter: Reporter):
    tasks = tuple(tasks)
    results = []
    STOP_EVENT.clear()

    aux_dirs = {
        os.path.join(command.cwd, ".aux")
        for task in tasks
        for command in task.commands
        if command.step is StepType.COMPILE
    }
    for aux_dir in aux_dirs:
        try:
            os.makedirs(aux_dir, exist_ok=True)
        except OSError as error:
            raise AutoLatexmkError(
                f"Failed to prepare auxiliary directory {aux_dir}: {error}"
            ) from error

    executor = ThreadPoolExecutor(max_workers=jobs)
    futures = {}
    try:
        for task in tasks:
            futures[executor.submit(run_task, task, timeout=timeout)] = task
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            reporter.task_finished(result)
    except KeyboardInterrupt:
        terminate_active_processes()
        completed = {result.task.index for result in results}
        for future, task in futures.items():
            if task.index in completed:
                continue
            result = future.result()
            results.append(result)
            reporter.task_finished(result)
        completed = {result.task.index for result in results}
        for task in tasks:
            if task.index in completed:
                continue
            result = run_task(task, timeout=timeout)
            results.append(result)
            reporter.task_finished(result)
        raise TaskExecutionInterrupted(results) from None
    finally:
        executor.shutdown(wait=True)
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.clear()
    return results
