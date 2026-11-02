import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from auto_latexmk.execution import (
    STOP_EVENT,
    TaskExecutionInterrupted,
    _extract_error,
    _signal_process_tree,
    _start_process,
    run_task,
    run_tasks,
)
from auto_latexmk.models import CommandSpec, LatexTask, RunSummary, StepType, TaskType
from auto_latexmk.reporting import Reporter


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        STOP_EVENT.clear()
        self.addCleanup(STOP_EVENT.clear)
        temporary = tempfile.TemporaryDirectory(prefix="auto latexmk ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tex = self.root / "main.tex"
        self.tex.write_text("\\documentclass{article}\n", encoding="utf-8")

    def task(self, code, *, clean_code=None):
        commands = []
        if clean_code is not None:
            commands.append(
                CommandSpec(
                    StepType.CLEAN,
                    str(self.root),
                    (sys.executable, "-c", clean_code),
                )
            )
        commands.append(
            CommandSpec(
                StepType.COMPILE,
                str(self.root),
                (sys.executable, "-c", code),
            )
        )
        return LatexTask(
            1,
            TaskType.REBUILD if clean_code is not None else TaskType.COMPILE,
            str(self.tex),
            "pdflatex",
            "command-line option",
            tuple(commands),
        )

    def read_output(self, result):
        self.assertIsNotNone(result.output_file)
        return Path(result.output_file).read_text(encoding="utf-8")

    def test_rebuild_preserves_both_steps_even_if_clean_deletes_aux(self):
        (self.root / ".aux").mkdir()
        result = run_task(
            self.task(
                "print('compile output')",
                clean_code="import shutil; shutil.rmtree('.aux'); print('clean output')",
            )
        )
        self.assertTrue(result.success)
        self.assertEqual(
            [step.command.step for step in result.steps],
            [StepType.CLEAN, StepType.COMPILE],
        )
        output = self.read_output(result)
        self.assertIn("Working directory:", output)
        self.assertIn("Engine: pdflatex", output)
        self.assertLess(output.index("Step: clean"), output.index("Step: compile"))
        self.assertIn("\nclean output\n", output)
        self.assertIn("\ncompile output\n", output)

    def test_clean_failure_skips_compile(self):
        result = run_task(
            self.task(
                "from pathlib import Path; Path('unexpected').touch()",
                clean_code="import sys; print('clean failed'); sys.exit(1)",
            )
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.steps[1].status, "skipped")
        self.assertFalse((self.root / "unexpected").exists())
        self.assertIn("clean failed", self.read_output(result))

    def test_failure_uses_current_output_instead_of_stale_tex_log(self):
        aux = self.root / ".aux"
        aux.mkdir()
        (aux / "main.log").write_text("! OLD ERROR\n", encoding="utf-8")
        result = run_task(
            self.task(
                "import sys; print('Latexmk: version information'); "
                "print('./sections/intro.tex:18: Undefined control sequence.'); sys.exit(12)"
            )
        )
        self.assertEqual(result.status, "failed")
        step = result.steps[0]
        self.assertIn("intro.tex:18", step.error_excerpt or "")
        self.assertNotIn("OLD ERROR", step.error_excerpt or "")
        self.assertIsNone(step.log_file)
        self.assertIn("Exit code: 12", self.read_output(result))

    def test_new_tex_log_is_linked(self):
        result = run_task(
            self.task(
                "from pathlib import Path; import sys; "
                "Path('.aux/main.log').write_text('new diagnostic'); "
                "print('! Undefined control sequence.'); sys.exit(1)"
            )
        )
        self.assertEqual(
            result.steps[0].log_file, (self.root / ".aux/main.log").as_posix()
        )

    def test_missing_executable_has_saved_startup_diagnostic(self):
        command = CommandSpec(
            StepType.COMPILE, str(self.root), ("auto-latexmk-nonexistent-tool",)
        )
        task = LatexTask(
            1, TaskType.COMPILE, str(self.tex), "pdflatex", "option", (command,)
        )
        result = run_task(task)
        self.assertFalse(result.success)
        self.assertIn("Could not start", result.steps[0].error_message or "")
        self.assertIn("Could not start", self.read_output(result))

    def test_latest_run_replaces_previous_capture(self):
        first = run_task(self.task("print('old output')"))
        second = run_task(self.task("print('new output')"))
        self.assertEqual(first.output_file, second.output_file)
        output = self.read_output(second)
        self.assertNotIn("old output", output)
        self.assertIn("new output", output)

    def test_batch_continues_after_one_document_fails(self):
        failed = self.task("import sys; sys.exit(1)")
        other = self.root / "other"
        other.mkdir()
        good = LatexTask(
            2,
            TaskType.COMPILE,
            str(other / "main.tex"),
            "pdflatex",
            "option",
            (
                CommandSpec(
                    StepType.COMPILE, str(other), (sys.executable, "-c", "print('OK')")
                ),
            ),
        )
        results = run_tasks((failed, good), jobs=2, reporter=Mock())
        self.assertEqual(
            {result.task.index: result.status for result in results},
            {1: "failed", 2: "succeeded"},
        )

    def test_log_save_failure_is_reported_without_hiding_compile_result(self):
        with patch(
            "auto_latexmk.execution.shutil.copyfileobj",
            side_effect=OSError("disk full"),
        ):
            result = run_task(self.task("print('OK')"))
        self.assertTrue(result.success)
        self.assertIsNone(result.output_file)
        self.assertIn("disk full", result.log_error or "")

    def test_interruption_saves_partial_output_and_stops_process(self):
        task = self.task(
            "from pathlib import Path; import time; "
            "print('partial output', flush=True); Path('ready').touch(); time.sleep(60)"
        )
        started = []
        ready = self.root / "ready"

        def start(command, output):
            process = _start_process(command, output)
            started.append(process)
            return process

        def interrupt_when_ready(_futures):
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready.exists():
                STOP_EVENT.set()
                raise AssertionError("child did not start")
            raise KeyboardInterrupt

        queued = LatexTask(
            2,
            task.task_type,
            str(self.root / "queued.tex"),
            task.engine,
            task.engine_reason,
            task.commands,
        )
        reporter = Mock()
        with (
            patch("auto_latexmk.execution._start_process", side_effect=start),
            patch(
                "auto_latexmk.execution.as_completed",
                side_effect=interrupt_when_ready,
            ),
            self.assertRaises(TaskExecutionInterrupted) as raised,
        ):
            run_tasks((task, queued), jobs=1, reporter=reporter)

        results = {result.task.index: result for result in raised.exception.results}
        self.assertEqual(set(results), {1, 2})
        self.assertTrue(all(result.cancelled for result in results.values()))
        self.assertIn("\npartial output\n", self.read_output(results[1]))
        self.assertIn("Status: cancelled", self.read_output(results[1]))
        self.assertIsNone(results[2].output_file)
        self.assertTrue(all(process.poll() is not None for process in started))
        self.assertEqual(reporter.task_finished.call_count, 2)

    def test_windows_tree_termination_uses_taskkill_tree_flag(self):
        process = Mock(pid=1234)
        process.poll.return_value = None
        with (
            patch("auto_latexmk.execution.os.name", "nt"),
            patch("auto_latexmk.execution.subprocess.run") as run,
        ):
            self.assertTrue(_signal_process_tree(process, force=True))
        self.assertEqual(
            run.call_args.args[0], ["taskkill", "/PID", "1234", "/T", "/F"]
        )

    def test_error_excerpt_skips_long_version_preamble(self):
        output = "Latexmk: version information\n" + "preamble\n" * 50
        output += "./main.tex:18: Undefined control sequence.\nl.18 error\n"
        excerpt = _extract_error(output)
        self.assertIn("main.tex:18", excerpt)
        self.assertNotIn("version information", excerpt)

    def test_error_excerpt_prioritizes_source_error_over_missing_file_warning(self):
        output = "Warning: optional file not found\n" + "preamble\n" * 50
        output += "./main.tex:18: Undefined control sequence.\nl.18 error\n"
        excerpt = _extract_error(output)
        self.assertIn("main.tex:18", excerpt)
        self.assertNotIn("optional file", excerpt)


class ReporterTests(unittest.TestCase):
    def test_nonterminal_output_has_summary_without_color(self):
        output = io.StringIO()
        reporter = Reporter(task_type=TaskType.COMPILE, stream=output)
        reporter.start(())
        reporter.finish(RunSummary(0.1, []))
        self.assertIn("No LaTeX main documents", output.getvalue())
        self.assertIn("0 succeeded, 0 failed, 0 cancelled", output.getvalue())
        self.assertNotIn("\033[", output.getvalue())

    def test_verbose_output_is_grouped_with_each_document(self):
        with tempfile.TemporaryDirectory() as directory:
            task = LatexTask(
                1,
                TaskType.COMPILE,
                str(Path(directory) / "main.tex"),
                "pdflatex",
                "option",
                (
                    CommandSpec(
                        StepType.COMPILE,
                        directory,
                        (sys.executable, "-c", "print('compiler output')"),
                    ),
                ),
            )
            STOP_EVENT.clear()
            result = run_task(task)
            output = io.StringIO()
            reporter = Reporter(task_type=TaskType.COMPILE, verbose=True, stream=output)
            reporter.start((task,))
            reporter.task_finished(result)
            reporter.finish(RunSummary(result.elapsed_time, [result]))
            text = output.getvalue()
            self.assertIn("Full output:", text)
            self.assertIn("Command:", text)
            self.assertIn("\ncompiler output\n", text)
            self.assertIn("1 succeeded", text)

    def test_no_color_disables_terminal_colors(self):
        stream = Mock()
        stream.isatty.return_value = True
        reporter = Reporter(task_type=TaskType.COMPILE, color=False, stream=stream)
        self.assertEqual(reporter._paint("FAILED", "31"), "FAILED")


if __name__ == "__main__":
    unittest.main()
