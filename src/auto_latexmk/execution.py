import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from auto_latexmk.models import LatexTask, StepResult, StepType, TaskResult
from auto_latexmk.reporting import Reporter
from auto_latexmk.runtime import logger

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
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
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
        process for process in processes if _signal_process_tree(process, force=False)
    ]
    time.sleep(0.2)
    for process in signalled:
        _signal_process_tree(process, force=True, allow_exited=os.name != "nt")


def _cancel_process(process):
    if not _signal_process_tree(process, force=False):
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
    return subprocess.Popen(
        command.argv,
        cwd=command.cwd,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )


def _log_signature(path):
    try:
        info = os.stat(path)
        return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino
    except OSError:
        return None


def _extract_error(text, *, max_lines=20):
    lines = text.splitlines()
    patterns = (
        (
            r"^!|:\d+:\s|(?:LaTeX|Package .+?) Error:|Undefined control sequence"
            r"|Emergency stop|Fatal error"
        ),
        r"not found|not recognized|I can't find|No such file",
    )
    for pattern in patterns:
        for index, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                start = max(0, index - 2)
                return "\n".join(lines[start : start + max_lines])
    return "\n".join(lines[-max_lines:])


def _write_output(output, text):
    output.write(text.encode("utf-8"))
    output.flush()


def run_step(task: LatexTask, command, *, output) -> StepResult:
    start_time = time.monotonic()
    rendered = (
        subprocess.list2cmdline(command.argv)
        if os.name == "nt"
        else shlex.join(command.argv)
    )
    _write_output(
        output,
        f"\nStep: {command.step.value}\nWorking directory: {command.cwd}\n"
        f"Command: {rendered}\n\n",
    )
    output_start = output.tell()
    jobname = os.path.splitext(os.path.basename(task.tex_file))[0]
    tex_log = os.path.join(command.cwd, ".aux", f"{jobname}.log")
    previous_log = _log_signature(tex_log)
    process = None
    status = "failed"
    message = None
    try:
        if STOP_EVENT.is_set():
            status, message = "cancelled", "Interrupted before start"
        else:
            if command.step is StepType.COMPILE:
                os.makedirs(os.path.join(command.cwd, ".aux"), exist_ok=True)
            process = _start_process(command, output)
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.add(process)
            while process.poll() is None:
                if STOP_EVENT.wait(0.1):
                    _cancel_process(process)
                    break
            if STOP_EVENT.is_set():
                status, message = "cancelled", "Interrupted by user"
            elif process.returncode == 0:
                status = "succeeded"
            else:
                message = (
                    f"{command.step.value} failed with exit code {process.returncode}"
                )
    except FileNotFoundError as error:
        message = f"Could not start {command.argv[0]}: {error}. Check the executable and working directory."
    except KeyboardInterrupt:
        STOP_EVENT.set()
        status, message = "cancelled", "Interrupted by user"
    except Exception as error:  # noqa: BLE001 - report individual document failures
        message = f"Could not run {command.step.value}: {error}"
    finally:
        if process is not None:
            if process.poll() is None:
                _cancel_process(process)
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.discard(process)

    output.seek(output_start)
    compiler_output = output.read().decode("utf-8", errors="replace")
    output.seek(0, os.SEEK_END)
    _write_output(
        output,
        f"\nStatus: {status}\n"
        + (f"{message}\n" if message else "")
        + (f"Exit code: {process.returncode}\n" if process is not None else ""),
    )
    current_log = _log_signature(tex_log)
    log_file = (
        tex_log.replace("\\", "/")
        if command.step is StepType.COMPILE
        and current_log is not None
        and current_log != previous_log
        else None
    )
    return StepResult(
        command=command,
        status=status,
        elapsed_time=time.monotonic() - start_time,
        exit_code=process.returncode if process is not None else None,
        error_message=message,
        error_excerpt=_extract_error(compiler_output)
        if status != "succeeded"
        else None,
        log_file=log_file,
    )


def _cancelled_task(task):
    return TaskResult(
        task,
        "cancelled",
        0.0,
        [
            StepResult(
                command,
                "cancelled" if index == 0 else "skipped",
                0.0,
                error_message="Interrupted before start" if index == 0 else None,
            )
            for index, command in enumerate(task.commands)
        ],
    )


def run_task(task: LatexTask) -> TaskResult:
    if STOP_EVENT.is_set():
        return _cancelled_task(task)
    start_time = time.monotonic()
    results = []
    status = "succeeded"
    output_file = None
    log_error = None
    try:
        # Keep capture outside .aux until cleanup finishes, including custom cleanup rules.
        with tempfile.TemporaryFile(mode="w+b") as output:
            _write_output(
                output,
                f"Document: {task.tex_file}\nOperation: {task.task_type.value}\n"
                f"Started: {datetime.now(UTC).isoformat()}\n"
                + (
                    f"Engine: {task.engine} ({task.engine_reason})\n"
                    if task.engine
                    else ""
                ),
            )
            for command in task.commands:
                if status != "succeeded":
                    results.append(StepResult(command, "skipped", 0.0))
                    continue
                result = run_step(task, command, output=output)
                results.append(result)
                status = result.status
            aux_dir = os.path.join(os.path.dirname(task.tex_file), ".aux")
            jobname = os.path.splitext(os.path.basename(task.tex_file))[0]
            destination = os.path.join(aux_dir, f"{jobname}.auto-latexmk.log")
            try:
                os.makedirs(aux_dir, exist_ok=True)
                output.seek(0)
                with open(destination, "wb") as saved:
                    shutil.copyfileobj(output, saved)
                output_file = destination.replace("\\", "/")
            except OSError as error:
                log_error = f"Could not save command output to {destination}: {error}"
    except Exception as error:  # noqa: BLE001 - capture/setup errors must not stop the batch
        status = "failed"
        results.append(
            StepResult(
                task.commands[0],
                "failed",
                time.monotonic() - start_time,
                error_message=f"Could not capture command output: {error}",
            )
        )
    return TaskResult(
        task,
        status,
        time.monotonic() - start_time,
        results,
        output_file,
        log_error,
    )


def run_tasks(tasks, *, jobs: int, reporter: Reporter):
    tasks = tuple(tasks)
    results = []
    STOP_EVENT.clear()
    executor = ThreadPoolExecutor(max_workers=jobs)
    futures = {}
    try:
        for task in tasks:
            futures[executor.submit(run_task, task)] = task
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            reporter.task_finished(result)
    except KeyboardInterrupt:
        terminate_active_processes()
        for future in futures:
            future.cancel()
        completed = {result.task.index for result in results}
        for future, task in futures.items():
            if task.index not in completed:
                result = (
                    _cancelled_task(task) if future.cancelled() else future.result()
                )
                results.append(result)
                reporter.task_finished(result)
        submitted = {task.index for task in futures.values()}
        for task in tasks:
            if task.index not in submitted:
                result = _cancelled_task(task)
                results.append(result)
                reporter.task_finished(result)
        raise TaskExecutionInterrupted(results) from None
    finally:
        executor.shutdown(wait=True)
        with ACTIVE_PROCESSES_LOCK:
            ACTIVE_PROCESSES.clear()
    return results
