import os
import shlex
import subprocess
import sys

from auto_latexmk.models import StepType, TaskPlan


def _relative_cwd(plan: TaskPlan, cwd: str) -> str:
    relative = os.path.relpath(cwd, plan.root_dir).replace("\\", "/")
    return "." if relative == "." else f"./{relative}"


def preview_plan(plan: TaskPlan, *, stream=None):
    stream = stream or sys.stdout
    total = len(plan.tasks)
    width = len(str(max(total, 1)))
    print(f"{total} task(s) planned:", file=stream)
    for task in plan.tasks:
        detail = task.engine or task.task_type.value
        print(
            f"  [{task.index:>{width}}/{total}] {task.tex_file} "
            f"({task.task_type.value}; {detail})",
            file=stream,
        )
        for command in task.commands:
            rendered = (
                subprocess.list2cmdline(command.argv)
                if os.name == "nt"
                else shlex.join(command.argv)
            )
            print(
                f"    {command.step.value}: cwd={command.cwd} {rendered}",
                file=stream,
            )


def _sh_task_line(plan: TaskPlan, task) -> str:
    cwd = shlex.quote(_relative_cwd(plan, task.commands[0].cwd))
    commands = [shlex.join(command.argv) for command in task.commands]
    if any(command.step is StepType.COMPILE for command in task.commands):
        commands.insert(0, "mkdir -p .aux")
    commands = " && ".join(commands)
    body = f"(cd {cwd} && {commands})"
    return (
        f"{body}; auto_latexmk_rc=$?; "
        '[ "$auto_latexmk_rc" -eq 0 ] || auto_latexmk_status=1; '
        '(exit "$auto_latexmk_rc")'
    )


def _pwsh_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pwsh_command(argv: tuple[str, ...]) -> str:
    return "& " + " ".join(_pwsh_quote(argument) for argument in argv)


def _pwsh_task_line(plan: TaskPlan, task) -> str:
    cwd = _relative_cwd(plan, task.commands[0].cwd)
    steps = []
    if any(command.step is StepType.COMPILE for command in task.commands):
        steps.append(
            "New-Item -ItemType Directory -Force -Path '.aux' | Out-Null"
        )
    for command in task.commands:
        steps.append(_pwsh_command(command.argv))
        steps.append(
            "if ($LASTEXITCODE -ne 0) { "
            "$script:autoLatexmkTaskFailed = $true; return }"
        )
    body = "; ".join(["$ErrorActionPreference = 'Stop'", *steps])
    return (
        "$script:autoLatexmkTaskFailed = $false; "
        f"& {{ try {{ Push-Location {_pwsh_quote(cwd)} -ErrorAction Stop; "
        f"try {{ {body} }} finally {{ Pop-Location }} }} "
        "catch { $script:autoLatexmkTaskFailed = $true; "
        "Write-Error $_ -ErrorAction Continue } }; "
        "if ($script:autoLatexmkTaskFailed) { $script:autoLatexmkFailed = $true }"
    )


def export_plan(plan: TaskPlan, *, stream=None, platform=None):
    stream = stream or sys.stdout
    platform = platform or ("windows" if os.name == "nt" else "posix")
    if platform == "windows":
        print("$script:autoLatexmkFailed = $false", file=stream)
        for task in plan.tasks:
            print(_pwsh_task_line(plan, task), file=stream)
        print("if ($script:autoLatexmkFailed) { exit 1 }", file=stream)
    else:
        print("#!/bin/sh", file=stream)
        print("auto_latexmk_status=0", file=stream)
        for task in plan.tasks:
            print(_sh_task_line(plan, task), file=stream)
        print('exit "$auto_latexmk_status"', file=stream)
    stream.flush()
