import io
import json
import unittest
from unittest.mock import Mock, patch

from auto_latexmk.actions import export_plan, preview_plan
from auto_latexmk.execution import (
    TaskExecutionInterrupted,
    _signal_process_tree,
    run_task,
    run_tasks,
)
from auto_latexmk.models import (
    CommandSpec,
    LatexTask,
    RunSummary,
    StepResult,
    StepType,
    TaskPlan,
    TaskResult,
    TaskType,
)
from auto_latexmk.reporting import JsonReporter, ListReporter


def make_plan(task_type=TaskType.CLEAN_COMPILE):
    clean = CommandSpec(StepType.CLEAN, "/project/notes", ("latexmk", "-c", "./main.tex"))
    compile_command = CommandSpec(
        StepType.COMPILE,
        "/project/notes",
        ("latexmk", "-pdfxe", "./main.tex"),
    )
    commands = (clean, compile_command) if task_type is TaskType.CLEAN_COMPILE else (compile_command,)
    task = LatexTask(1, task_type, "/project/notes/main.tex", "xelatex", "fallback", commands)
    return TaskPlan("/project", task_type, (task,))


class ActionTests(unittest.TestCase):
    def test_preview_is_fixed_human_list(self):
        stream = io.StringIO()
        preview_plan(make_plan(), stream=stream)
        output = stream.getvalue()
        self.assertIn("1 task(s) planned", output)
        self.assertIn("clean-compile", output)
        self.assertIn("clean:", output)
        self.assertIn("compile:", output)

    def test_preview_defaults_to_stdout(self):
        stream = io.StringIO()
        with patch("auto_latexmk.actions.sys.stdout", stream):
            preview_plan(make_plan())
        self.assertIn("1 task(s) planned", stream.getvalue())

    def test_posix_export_is_relocatable_and_aggregates_failures(self):
        stream = io.StringIO()
        export_plan(make_plan(), stream=stream, platform="posix")
        output = stream.getvalue()
        self.assertIn("#!/bin/sh", output)
        self.assertIn("cd ./notes", output)
        self.assertIn(
            "mkdir -p .aux && latexmk -c ./main.tex && latexmk -pdfxe ./main.tex",
            output,
        )
        self.assertIn("auto_latexmk_status=1", output)
        self.assertIn('exit "$auto_latexmk_status"', output)
        self.assertNotIn("/project", output)

    def test_powershell_export_uses_current_platform_syntax(self):
        stream = io.StringIO()
        export_plan(make_plan(), stream=stream, platform="windows")
        output = stream.getvalue()
        self.assertIn("Push-Location './notes'", output)
        self.assertIn("$LASTEXITCODE", output)
        self.assertIn("Pop-Location", output)
        self.assertIn("Push-Location './notes' -ErrorAction Stop", output)
        self.assertIn("$ErrorActionPreference = 'Stop'", output)
        self.assertIn("catch {", output)
        self.assertIn("exit 1", output)

    def test_empty_export_is_successful_script(self):
        stream = io.StringIO()
        export_plan(TaskPlan("/project", TaskType.COMPILE, ()), stream=stream, platform="posix")
        self.assertEqual(
            stream.getvalue(),
            '#!/bin/sh\nauto_latexmk_status=0\nexit "$auto_latexmk_status"\n',
        )


class JsonV2Tests(unittest.TestCase):
    def test_json_contains_structured_step_error(self):
        plan = make_plan(TaskType.COMPILE)
        command = plan.tasks[0].commands[0]
        step = StepResult(
            command,
            "failed",
            0.8,
            exit_code=12,
            error_kind="latex-log",
            error_message="compile failed",
            error_excerpt="Undefined control sequence",
            log_file="/project/notes/.aux/main.log",
        )
        result = TaskResult(plan.tasks[0], "failed", 0.81, [step])
        summary = RunSummary(TaskType.COMPILE, "failed", 0.82, [result])
        stream = io.StringIO()
        JsonReporter(task_type=TaskType.COMPILE, stream=stream).finish(summary)
        data = json.loads(stream.getvalue())
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["summary"]["failed"], 1)
        self.assertEqual(data["tasks"][0]["steps"][0]["argv"], list(command.argv))
        self.assertEqual(data["tasks"][0]["steps"][0]["error"]["kind"], "latex-log")


class ReporterChannelTests(unittest.TestCase):
    def test_list_reporter_defaults_to_stdout(self):
        stream = io.StringIO()
        with patch("auto_latexmk.reporting.sys.stdout", stream):
            reporter = ListReporter(task_type=TaskType.COMPILE, color=False)
        self.assertIs(reporter.stream, stream)

    def test_list_reporter_writes_fatal_diagnostics_to_stderr(self):
        output = io.StringIO()
        diagnostics = io.StringIO()
        reporter = ListReporter(
            task_type=TaskType.COMPILE,
            color=False,
            stream=output,
        )
        with patch("auto_latexmk.reporting.sys.stderr", diagnostics):
            reporter.fatal_error("setup failed")
            reporter.interrupted()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(
            diagnostics.getvalue(),
            "ERROR: setup failed\nInterrupted.\n",
        )


class ExecutionTests(unittest.TestCase):
    def test_clean_compile_runs_steps_in_order(self):
        task = make_plan().tasks[0]

        def succeed(_task, command, *, timeout):
            return StepResult(command, "succeeded", 0.1, exit_code=0)

        with patch("auto_latexmk.execution.run_step", side_effect=succeed) as run:
            result = run_task(task, timeout=10)

        self.assertTrue(result.success)
        self.assertEqual([call.args[1].step for call in run.call_args_list], [StepType.CLEAN, StepType.COMPILE])

    def test_clean_failure_skips_compile(self):
        task = make_plan().tasks[0]

        def fail(_task, command, *, timeout):
            return StepResult(
                command,
                "failed",
                0.1,
                exit_code=1,
                error_kind="process-output",
                error_message="clean failed",
            )

        with patch("auto_latexmk.execution.run_step", side_effect=fail) as run:
            result = run_task(task, timeout=10)

        self.assertEqual(result.status, "failed")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.steps[1].status, "skipped")

    def test_interruption_preserves_results_from_all_planned_tasks(self):
        base = make_plan(TaskType.COMPILE).tasks[0]
        tasks = tuple(
            LatexTask(
                index,
                base.task_type,
                f"/project/notes/main-{index}.tex",
                base.engine,
                base.engine_reason,
                (),
            )
            for index in (1, 2)
        )
        reporter = Mock()

        with patch(
            "auto_latexmk.execution.as_completed",
            side_effect=KeyboardInterrupt,
        ), self.assertRaises(TaskExecutionInterrupted) as raised:
            run_tasks(tasks, jobs=2, timeout=10, reporter=reporter)

        self.assertEqual(
            {result.task.index for result in raised.exception.results},
            {1, 2},
        )
        self.assertEqual(reporter.task_finished.call_count, 2)

    def test_windows_tree_termination_uses_taskkill_tree_flag(self):
        process = Mock(pid=1234)
        process.poll.return_value = None

        with patch("auto_latexmk.execution.os.name", "nt"), patch(
            "auto_latexmk.execution.subprocess.run"
        ) as run:
            signalled = _signal_process_tree(process, force=True)

        self.assertTrue(signalled)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["taskkill", "/PID"])
        self.assertIn("/T", command)
        self.assertIn("/F", command)


if __name__ == "__main__":
    unittest.main()
