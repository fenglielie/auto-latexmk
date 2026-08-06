import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LATEXMK_AVAILABLE = shutil.which("latexmk") is not None
PDFLATEX_AVAILABLE = shutil.which("pdflatex") is not None
XELATEX_AVAILABLE = shutil.which("xelatex") is not None
FIXTURES = Path(__file__).with_name("fixtures")


def tex_file_available(name):
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return False
    try:
        completed = subprocess.run(
            [kpsewhich, name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


CTEX_AVAILABLE = tex_file_available("ctexart.cls")


class CliIntegrationTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def copy_project(self, name):
        shutil.copytree(FIXTURES / name, self.root, dirs_exist_ok=True)
        if name == "english-project":
            (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")

    def run_cli(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "auto_latexmk.auto_latexmk",
                *arguments,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )

    def run_json(self, *arguments):
        completed = self.run_cli(
            "--output-mode",
            "json",
            "--timeout",
            "90",
            *arguments,
            str(self.root),
        )
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"CLI did not emit valid JSON: {error}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed, data


@unittest.skipUnless(
    LATEXMK_AVAILABLE and PDFLATEX_AVAILABLE,
    "requires latexmk and pdflatex on PATH",
)
class PdfLatexCliIntegrationTests(CliIntegrationTestCase):
    def test_compile_clean_and_clean_compile_lifecycle(self):
        self.copy_project("english-project")

        compile_run, compile_data = self.run_json(
            "--engine", "pdflatex", "--no-engine-cache"
        )
        self.assertEqual(compile_run.returncode, 0, compile_run.stderr)
        self.assertEqual(compile_data["schema_version"], 2)
        self.assertEqual(compile_data["status"], "succeeded")
        self.assertEqual(compile_data["summary"]["total"], 2)
        self.assertEqual(
            {Path(task["path"]).relative_to(self.root).as_posix() for task in compile_data["tasks"]},
            {"notes/main.tex", "report/main.tex"},
        )
        self.assertTrue(all(task["status"] == "succeeded" for task in compile_data["tasks"]))
        self.assertTrue((self.root / "notes" / "main.pdf").is_file())
        self.assertTrue((self.root / "report" / "main.pdf").is_file())
        self.assertTrue((self.root / "notes" / ".aux" / "main.log").is_file())
        self.assertTrue((self.root / "report" / ".aux" / "main.log").is_file())
        self.assertFalse((self.root / "ignored" / "draft.pdf").exists())

        clean_run, clean_data = self.run_json("--task", "clean")
        self.assertEqual(clean_run.returncode, 0, clean_run.stderr)
        self.assertEqual(clean_data["status"], "succeeded")
        self.assertEqual(clean_data["summary"]["total"], 2)
        self.assertTrue(
            all(task["steps"][0]["step"] == "clean" for task in clean_data["tasks"])
        )
        self.assertTrue((self.root / "notes" / "main.pdf").is_file())
        self.assertTrue((self.root / "report" / "main.pdf").is_file())
        self.assertFalse((self.root / "notes" / ".aux" / "main.aux").exists())
        self.assertFalse((self.root / "report" / ".aux" / "main.aux").exists())

        rebuild_run, rebuild_data = self.run_json(
            "--task",
            "clean-compile",
            "--engine",
            "pdflatex",
            "--no-engine-cache",
        )
        self.assertEqual(rebuild_run.returncode, 0, rebuild_run.stderr)
        self.assertEqual(rebuild_data["status"], "succeeded")
        self.assertTrue(
            all(
                [step["step"] for step in task["steps"]] == ["clean", "compile"]
                for task in rebuild_data["tasks"]
            )
        )
        self.assertTrue((self.root / "notes" / "main.pdf").is_file())
        self.assertTrue((self.root / "report" / "main.pdf").is_file())

    def test_exported_script_compiles_without_auto_latexmk(self):
        self.copy_project("english-project")
        exported = self.run_cli(
            "--action",
            "export",
            "--engine",
            "pdflatex",
            "--no-engine-cache",
            str(self.root),
        )
        self.assertEqual(exported.returncode, 0, exported.stderr)

        if os.name == "nt":
            shell = shutil.which("pwsh")
            if shell is None:
                self.skipTest("Windows export execution requires pwsh")
            script = self.root / "commands.ps1"
            command = [shell, "-NoProfile", "-File", str(script)]
            self.assertIn("Push-Location", exported.stdout)
        else:
            shell = shutil.which("sh")
            if shell is None:
                self.skipTest("POSIX export execution requires sh")
            script = self.root / "commands.sh"
            command = [shell, str(script)]
            self.assertTrue(exported.stdout.startswith("#!/bin/sh\n"))

        script.write_text(exported.stdout, encoding="utf-8")
        executed = subprocess.run(
            command,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        self.assertEqual(
            executed.returncode,
            0,
            f"stdout:\n{executed.stdout}\nstderr:\n{executed.stderr}",
        )
        self.assertTrue((self.root / "notes" / "main.pdf").is_file())
        self.assertTrue((self.root / "report" / "main.pdf").is_file())
        self.assertTrue((self.root / "notes" / ".aux" / "main.log").is_file())
        self.assertTrue((self.root / "report" / ".aux" / "main.log").is_file())
        self.assertFalse((self.root / "ignored" / "draft.pdf").exists())

    def test_failed_compile_emits_machine_readable_diagnostics(self):
        self.copy_project("failing-project")

        completed, data = self.run_json(
            "--engine", "pdflatex", "--no-engine-cache"
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["summary"]["failed"], 1)
        step = data["tasks"][0]["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertIsInstance(step["argv"], list)
        self.assertEqual(step["exit_code"], 12)
        self.assertIn(step["error"]["kind"], {"latex-log", "process-output"})
        self.assertIn("Undefined control sequence", step["error"]["excerpt"])
        self.assertTrue(step["error"]["log_file"].endswith("main.log"))


@unittest.skipUnless(
    LATEXMK_AVAILABLE and XELATEX_AVAILABLE and CTEX_AVAILABLE,
    "requires latexmk, xelatex, and ctexart.cls",
)
class XeLatexCliIntegrationTests(CliIntegrationTestCase):
    def test_chinese_document_uses_directive_and_compiles(self):
        self.copy_project("chinese-project")

        completed, data = self.run_json("--no-engine-cache")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(data["status"], "succeeded")
        task = data["tasks"][0]
        self.assertEqual(task["engine"], "xelatex")
        self.assertEqual(task["engine_reason"], "% !TEX program = xelatex")
        self.assertIn("-pdfxe", task["steps"][0]["argv"])
        self.assertTrue(task["path"].endswith("/thesis/main.tex"))
        self.assertTrue((self.root / "thesis" / "main.pdf").is_file())
        self.assertTrue((self.root / "thesis" / ".aux" / "main.log").is_file())


if __name__ == "__main__":
    unittest.main()
