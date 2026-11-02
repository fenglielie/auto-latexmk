import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from auto_latexmk.cli import clean_main, main, parse_args, rebuild_main
from auto_latexmk.models import TaskType


class CLIParsingTests(unittest.TestCase):
    def test_compile_and_rebuild_defaults(self):
        for task_type in (TaskType.COMPILE, TaskType.REBUILD):
            with self.subTest(task_type=task_type):
                args = parse_args([], task_type=task_type)
                self.assertEqual(args.path, os.getcwd())
                self.assertEqual(args.engine, "auto")
                self.assertGreaterEqual(args.jobs, 1)
                self.assertLessEqual(args.jobs, 4)
                self.assertTrue(args.use_gitignore)
                self.assertFalse(args.no_engine_cache)
                self.assertFalse(args.no_color)
                self.assertFalse(args.verbose)

    def test_clean_only_accepts_relevant_options(self):
        args = parse_args(
            ["--no-gitignore", "--no-color", "--verbose"], task_type=TaskType.CLEAN
        )
        self.assertTrue(args.no_engine_cache)
        self.assertEqual(args.jobs, 1)
        self.assertFalse(args.use_gitignore)
        for option in (
            ["--engine", "auto"],
            ["-pdf"],
            ["-pdfxe"],
            ["-pdflua"],
            ["--no-engine-cache"],
            ["-j", "2"],
        ):
            self.assert_parse_error(option, task_type=TaskType.CLEAN)

    def test_engine_aliases_and_switches(self):
        for task_type in (TaskType.COMPILE, TaskType.REBUILD):
            for flag, engine in (
                ("-pdfxe", "xelatex"),
                ("-pdf", "pdflatex"),
                ("-pdflua", "lualatex"),
            ):
                with self.subTest(task_type=task_type, flag=flag):
                    args = parse_args(
                        [
                            flag,
                            "--no-engine-cache",
                            "--no-color",
                            "--no-gitignore",
                            "--verbose",
                        ],
                        task_type=task_type,
                    )
                    self.assertEqual(args.engine, engine)
                    self.assertTrue(args.no_engine_cache)
                    self.assertTrue(args.no_color)
                    self.assertFalse(args.use_gitignore)
                    self.assertTrue(args.verbose)

    def test_engine_options_are_mutually_exclusive(self):
        self.assert_parse_error(["--engine", "xelatex", "-pdfxe"])
        self.assert_parse_error(["-pdfxe", "-pdf"])

    def test_removed_options_are_rejected(self):
        for task_type in TaskType:
            for arguments in (
                ["--task", "clean"],
                ["--action", "preview"],
                ["--export-format", "pwsh"],
                ["--output-mode", "json"],
                ["--debug-log", "debug.log"],
                ["--timeout", "180"],
            ):
                with self.subTest(task_type=task_type, arguments=arguments):
                    self.assert_parse_error(arguments, task_type=task_type)

    def test_positive_jobs_are_required(self):
        self.assert_parse_error(["--jobs", "0"])
        self.assert_parse_error(["--jobs", "-1"])

    def test_exclude_is_repeatable(self):
        args = parse_args(["-x", "ref*", "-x", "*-old.tex", "project"])
        self.assertEqual(args.exclude, ["ref*", "*-old.tex"])
        self.assertEqual(args.path, "project")

    def test_help_is_specific_to_each_command(self):
        for task_type, program in (
            (TaskType.COMPILE, "auto-latexmk"),
            (TaskType.CLEAN, "auto-latexmk-clean"),
            (TaskType.REBUILD, "auto-latexmk-rebuild"),
        ):
            output = StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                parse_args(["--help"], task_type=task_type)
            self.assertEqual(raised.exception.code, 0)
            help_text = output.getvalue()
            self.assertIn(f"usage: {program} ", help_text)
            self.assertIn("--verbose", help_text)
            self.assertNotIn("--action", help_text)
            self.assertEqual("--engine" in help_text, task_type is not TaskType.CLEAN)

    def test_entry_points_select_their_operation(self):
        for entry, task_type in (
            (main, TaskType.COMPILE),
            (clean_main, TaskType.CLEAN),
            (rebuild_main, TaskType.REBUILD),
        ):
            with patch("auto_latexmk.cli._run", return_value=0) as run:
                self.assertEqual(entry(["main.tex"]), 0)
                run.assert_called_once_with(task_type, ["main.tex"])

    def assert_parse_error(self, argv, *, task_type=TaskType.COMPILE):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            parse_args(argv, task_type=task_type)
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
