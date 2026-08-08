import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from auto_latexmk.cli import parse_args
from auto_latexmk.models import Action, TaskType


class CLIParsingTests(unittest.TestCase):
    def test_defaults(self):
        args = parse_args([])
        self.assertIs(args.task, TaskType.COMPILE)
        self.assertIs(args.action, Action.RUN)
        self.assertEqual(args.output_mode, "progressbar")
        self.assertEqual(args.engine, "auto")
        self.assertEqual(args.timeout, 180)

    def test_task_and_action_choices(self):
        args = parse_args(["--task", "clean-compile", "--action", "preview"])
        self.assertIs(args.task, TaskType.CLEAN_COMPILE)
        self.assertIs(args.action, Action.PREVIEW)
        self.assertIsNone(args.output_mode)

    def test_engine_aliases(self):
        cases = [
            ("-pdfxe", "xelatex"),
            ("-pdf", "pdflatex"),
            ("-pdflua", "lualatex"),
        ]
        for option, engine in cases:
            with self.subTest(option=option):
                self.assertEqual(parse_args([option]).engine, engine)

    def test_engine_options_are_mutually_exclusive(self):
        self.assert_parse_error(["--engine", "xelatex", "-pdfxe"])
        self.assert_parse_error(["-pdfxe", "-pdf"])

    def test_clean_rejects_engine_options_and_cache_switch(self):
        self.assert_parse_error(["--task", "clean", "--engine", "auto"])
        self.assert_parse_error(["--task", "clean", "--no-engine-cache"])

    def test_non_run_actions_reject_run_options(self):
        for action in ("preview", "export"):
            for option in (
                ["--output-mode", "list"],
                ["--jobs", "2"],
                ["--timeout", "10"],
            ):
                with self.subTest(action=action, option=option):
                    self.assert_parse_error(["--action", action, *option])

    def test_machine_outputs_reject_no_color(self):
        self.assert_parse_error(["--output-mode", "json", "--no-color"])
        self.assert_parse_error(["--action", "export", "--no-color"])

    def test_export_format_defaults_to_auto(self):
        args = parse_args(["--action", "export"])
        self.assertEqual(args.export_format, "auto")

    def test_export_format_explicit_values(self):
        for value in ("pwsh", "bash"):
            with self.subTest(value=value):
                args = parse_args(["--action", "export", "--export-format", value])
                self.assertEqual(args.export_format, value)

    def test_export_format_rejected_without_export_action(self):
        for action in ("run", "preview"):
            with self.subTest(action=action):
                self.assert_parse_error(["--action", action, "--export-format", "pwsh"])
                self.assert_parse_error(["--action", action, "--export-format", "bash"])
        # auto (default) is always accepted regardless of action
        args = parse_args(["--action", "run", "--export-format", "auto"])
        self.assertEqual(args.export_format, "auto")

    def test_positive_execution_values_are_required(self):
        self.assert_parse_error(["--jobs", "0"])
        self.assert_parse_error(["--timeout", "0"])

    def test_exclude_is_repeatable(self):
        args = parse_args(["-x", "ref*", "-x", "*-old.tex", "project"])
        self.assertEqual(args.exclude, ["ref*", "*-old.tex"])
        self.assertEqual(args.path, "project")

    def test_help_describes_scopes_channels_and_examples(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = output.getvalue()
        for expected in (
            "Steps to construct per document",
            "Equivalent to --engine xelatex",
            "Maximum concurrent document tasks",
            "Do not apply nested .gitignore rules",
            "Task/action model:",
            "Output channels:",
            "Scope rules:",
            "--task clean-compile --action preview",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, help_text)

    def assert_parse_error(self, argv):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            parse_args(argv)
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
