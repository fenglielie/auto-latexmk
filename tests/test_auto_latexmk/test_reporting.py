import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from auto_latexmk.auto_latexmk import (
    CompileResult,
    CompileTask,
    JsonReporter,
    ListReporter,
    ProgressBarReporter,
    Reporter,
    RunSummary,
    create_reporter,
    main,
    parse_args,
    run_compile_tasks,
)


class NonTTYStringIO(io.StringIO):
    def isatty(self):
        return False


class TTYStringIO(io.StringIO):
    def isatty(self):
        return True


def make_task(index=1, name="main.tex"):
    return CompileTask(
        index=index,
        tex_file=name,
        subdir=".",
        engine="xelatex",
        timeout=180,
    )


class ReporterTests(unittest.TestCase):
    def test_factory_creates_one_reporter_per_output_mode(self):
        expected_classes = {
            "progressbar": ProgressBarReporter,
            "list": ListReporter,
            "json": JsonReporter,
        }
        for output_mode, expected_class in expected_classes.items():
            with self.subTest(output_mode=output_mode):
                reporter = create_reporter(output_mode, color=False)
                self.assertIsInstance(reporter, Reporter)
                self.assertIsInstance(reporter, expected_class)

    def test_json_reporter_is_structured_and_stably_ordered(self):
        stream = io.StringIO()
        reporter = JsonReporter(stream=stream)
        results = [
            CompileResult(make_task(2, "b.tex"), "succeeded", 0.2, []),
            CompileResult(make_task(1, "a.tex"), "failed", 0.1, [], error="bad"),
        ]

        reporter.finish(RunSummary("failed", 0.3, results))
        data = json.loads(stream.getvalue())

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["summary"]["failed"], 1)
        self.assertEqual(
            [task["path"] for task in data["tasks"]],
            ["a.tex", "b.tex"],
        )

    def test_list_reporter_uses_stable_lines_without_tty(self):
        stream = NonTTYStringIO()
        reporter = ListReporter(color=False, stream=stream)
        tasks = [make_task(index=i, name=f"{i}.tex") for i in range(1, 30)]
        task = tasks[0]
        result = CompileResult(task, "succeeded", 0.25, [])

        reporter.start(tasks)
        reporter.task_finished(result)
        reporter.finish(RunSummary("succeeded", 0.25, [result]))

        output = stream.getvalue()
        self.assertIn("[ 1/29] OK", output)
        self.assertIn("1.tex (xelatex) (0.25s)", output)
        self.assertIn("1 succeeded, 0 failed", output)
        self.assertNotIn("\033[", output)

    def test_list_reporter_prints_failure_status_immediately(self):
        stream = NonTTYStringIO()
        reporter = ListReporter(color=False, stream=stream)
        tasks = [make_task(index=i, name=f"{i}.tex") for i in range(1, 30)]
        result = CompileResult(tasks[19], "failed", 0.61, [])

        reporter.start(tasks)
        for task in tasks[:19]:
            reporter.task_finished(CompileResult(task, "succeeded", 0.1, []))
        reporter.task_finished(result)

        self.assertIn(
            "[20/29] FAILED 20.tex (xelatex) (0.61s)",
            stream.getvalue(),
        )

    def test_console_reporters_show_failure_details_at_end(self):
        reporter_classes = (ProgressBarReporter, ListReporter)
        for reporter_class in reporter_classes:
            with self.subTest(reporter_class=reporter_class.__name__):
                stream = NonTTYStringIO()
                reporter = reporter_class(color=False, stream=stream)
                task = make_task()
                result = CompileResult(
                    task,
                    "failed",
                    0.61,
                    ["latexmk"],
                    error="Missing \\begin{document}.",
                    log_file=".aux/main.log",
                )

                reporter.start([task])
                reporter.task_finished(result)
                reporter.finish(RunSummary("failed", 0.61, [result]))

                output = stream.getvalue()
                self.assertIn(
                    "FAILED  main.tex (xelatex) (0.61s)",
                    output,
                )
                self.assertIn("  Missing \\begin{document}.", output)
                self.assertIn("  Log: .aux/main.log", output)

    @patch("auto_latexmk.auto_latexmk.tqdm")
    def test_progress_can_be_forced_without_tty(self, mock_tqdm):
        bar = mock_tqdm.return_value
        reporter = ProgressBarReporter(
            color=False,
            stream=NonTTYStringIO(),
        )
        task = make_task()

        reporter.start([task])
        reporter.task_finished(CompileResult(task, "succeeded", 0.1, []))
        reporter.close()

        mock_tqdm.assert_called_once()
        kwargs = mock_tqdm.call_args.kwargs
        self.assertEqual(kwargs["desc"], "Compile")
        self.assertIn("{bar:20}", kwargs["bar_format"])
        self.assertIn("[{elapsed}<{remaining}]", kwargs["bar_format"])
        self.assertIsNone(kwargs["colour"])
        self.assertNotIn("dynamic_ncols", kwargs)
        self.assertTrue(bar.bar_format.endswith(" 0 LEFT 1 OK 0 FAILED"))
        bar.update.assert_called_once_with(1)

    @patch("auto_latexmk.auto_latexmk.tqdm")
    def test_progress_bar_is_green_on_color_terminal(self, mock_tqdm):
        reporter = ProgressBarReporter(
            color=True,
            stream=TTYStringIO(),
        )

        reporter.start([make_task()])

        self.assertEqual(mock_tqdm.call_args.kwargs["colour"], "green")

    @patch("auto_latexmk.auto_latexmk.tqdm")
    def test_progress_bar_reserves_width_for_all_status_counts(self, mock_tqdm):
        reporter = ProgressBarReporter(
            color=False,
            stream=NonTTYStringIO(),
        )
        tasks = [make_task(index=i) for i in range(1, 30)]

        reporter.start(tasks)
        bar = mock_tqdm.return_value
        initial_format = mock_tqdm.call_args.kwargs["bar_format"]
        self.assertNotIn("{n_fmt}", initial_format)
        self.assertTrue(initial_format.endswith("29 LEFT  0 OK  0 FAILED"))

        for task in tasks[:20]:
            reporter.task_finished(CompileResult(task, "succeeded", 0.1, []))
        self.assertTrue(bar.bar_format.endswith(" 9 LEFT 20 OK  0 FAILED"))

    @patch("auto_latexmk.auto_latexmk.tqdm")
    def test_progress_bar_shows_actual_running_jobs_only_when_concurrent(
        self, mock_tqdm
    ):
        reporter = ProgressBarReporter(
            color=False,
            stream=NonTTYStringIO(),
        )
        reporter.start([make_task()])
        bar = mock_tqdm.return_value
        self.assertNotIn(
            "parallel jobs", mock_tqdm.call_args.kwargs["bar_format"]
        )

        reporter.running_changed(4)
        self.assertTrue(bar.bar_format.endswith(" (4 parallel jobs)"))

        reporter.running_changed(1)
        self.assertNotIn("parallel jobs", bar.bar_format)

        reporter.running_changed(0)
        self.assertNotIn("parallel jobs", bar.bar_format)

    def test_runner_reports_actual_running_task_count(self):
        tasks = [make_task(index=index) for index in range(1, 4)]
        reporter = MagicMock()
        first_workers_ready = threading.Barrier(2)

        def compile_task(task):
            if task.index <= 2:
                first_workers_ready.wait(timeout=2)
            return CompileResult(task, "succeeded", 0.1, [])

        with patch(
            "auto_latexmk.auto_latexmk.run_single_compile_task",
            side_effect=compile_task,
        ):
            results = run_compile_tasks(tasks, jobs=2, reporter=reporter)

        running_counts = [
            call.args[0] for call in reporter.running_changed.call_args_list
        ]
        self.assertEqual(len(results), 3)
        self.assertEqual(max(running_counts), 2)
        self.assertEqual(running_counts[-1], 0)
        self.assertEqual(reporter.task_finished.call_count, 3)

    @patch("auto_latexmk.auto_latexmk.tqdm")
    def test_progress_reports_failure_immediately(self, mock_tqdm):
        bar = mock_tqdm.return_value
        stream = TTYStringIO()
        reporter = ProgressBarReporter(color=True, stream=stream)
        task = make_task()

        reporter.start([task])
        reporter.task_finished(CompileResult(task, "failed", 0.1, []))

        self.assertTrue(bar.bar_format.endswith(" 0 LEFT 0 OK 1 FAILED"))
        self.assertEqual(bar.colour, "red")
        bar.refresh.assert_called_once_with()

    @patch("auto_latexmk.auto_latexmk.tqdm")
    def test_progress_finish_does_not_repeat_summary(self, mock_tqdm):
        stream = TTYStringIO()
        reporter = ProgressBarReporter(color=True, stream=stream)
        task = make_task()
        result = CompileResult(task, "succeeded", 0.1, [])

        reporter.start([task])
        reporter.task_finished(result)
        reporter.finish(RunSummary("succeeded", 0.1, [result]))

        self.assertNotIn("Compiled", stream.getvalue())
        mock_tqdm.return_value.close.assert_called_once_with()


class CLIOutputTests(unittest.TestCase):
    def test_output_mode_defaults_to_progressbar(self):
        self.assertEqual(parse_args([]).output_mode, "progressbar")

    def test_output_mode_accepts_all_modes(self):
        for mode in ("progressbar", "list", "json"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    parse_args(["--output-mode", mode]).output_mode,
                    mode,
                )

    def test_json_dry_run_keeps_stderr_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_file = os.path.join(temp_dir, "main.tex")
            with open(tex_file, "w", encoding="utf-8") as f:
                f.write("\\documentclass{article}\n")

            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "auto-latexmk",
                "--output-mode",
                "json",
                "--dry-run",
                "--no-cache-pref",
                tex_file,
            ]
            with (
                patch("sys.argv", argv),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "dry-run")

    def test_debug_log_is_independent_from_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_file = os.path.join(temp_dir, "main.tex")
            debug_file = os.path.join(temp_dir, "debug.log")
            with open(tex_file, "w", encoding="utf-8") as f:
                f.write("\\documentclass{article}\n")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write("old debug content\n")

            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "auto-latexmk",
                "--output-mode",
                "json",
                "--dry-run",
                "--no-cache-pref",
                "--debug-log",
                debug_file,
                tex_file,
            ]
            with (
                patch("sys.argv", argv),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main()

            with open(debug_file, encoding="utf-8") as f:
                debug_content = f.read()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "dry-run")
        self.assertIn("Discovered 1 task(s)", debug_content)
        self.assertNotIn("old debug content", debug_content)

    def test_debug_log_creation_failure_is_clean_json(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "auto-latexmk",
            "--output-mode",
            "json",
            "--debug-log",
            "debug.log",
        ]
        with (
            patch("sys.argv", argv),
            patch(
                "auto_latexmk.auto_latexmk.configure_debug_logging",
                side_effect=[OSError("access denied"), None],
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(data["status"], "failed")
        self.assertIn("Failed to create debug log", data["error"])

    def test_clean_only_missing_path_returns_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = os.path.join(temp_dir, "missing")
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "auto-latexmk",
                "--output-mode",
                "json",
                "--no-cache-pref",
                "--pre-clean",
                "--no-compile",
                missing_path,
            ]
            with (
                patch("sys.argv", argv),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(data["status"], "failed")
        self.assertIn("does not exist", data["error"])

    def test_human_dry_run_keeps_stdout_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_file = os.path.join(temp_dir, "main.tex")
            with open(tex_file, "w", encoding="utf-8") as f:
                f.write("\\documentclass{article}\n")

            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "auto-latexmk",
                "--dry-run",
                "--output-mode",
                "list",
                "--no-cache-pref",
                tex_file,
            ]
            with (
                patch("sys.argv", argv),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("1 task(s) planned", stderr.getvalue())

    def test_compile_failure_returns_one_and_clean_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_file = os.path.join(temp_dir, "main.tex")
            with open(tex_file, "w", encoding="utf-8") as f:
                f.write("\\documentclass{article}\n")

            def fail(tasks, *, jobs, reporter):
                result = CompileResult(
                    tasks[0],
                    "failed",
                    0.1,
                    ["latexmk"],
                    error="compile failed",
                )
                reporter.task_finished(result)
                return [result]

            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "auto-latexmk",
                "--output-mode",
                "json",
                "--no-cache-pref",
                tex_file,
            ]
            with (
                patch("sys.argv", argv),
                patch(
                    "auto_latexmk.auto_latexmk.run_compile_tasks",
                    side_effect=fail,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["summary"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
