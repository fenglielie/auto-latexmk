import shutil
import subprocess
import sysconfig
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
        temporary = tempfile.TemporaryDirectory(prefix="auto latexmk ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def copy_project(self, name):
        shutil.copytree(FIXTURES / name, self.root, dirs_exist_ok=True)
        if name == "english-project":
            (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")

    def run_cli(self, command="auto-latexmk", *arguments):
        entry = shutil.which(command, path=sysconfig.get_path("scripts"))
        if entry is None:
            self.fail(f"Missing console entry point: {command}; run uv sync")
        return subprocess.run(
            [entry, "--no-color", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )

    def output(self, completed):
        return completed.stdout + completed.stderr


class ConsoleEntryTests(CliIntegrationTestCase):
    def test_all_installed_commands_have_help_and_version(self):
        for command in ("auto-latexmk", "auto-latexmk-clean", "auto-latexmk-rebuild"):
            with self.subTest(command=command):
                help_run = self.run_cli(command, "--help")
                self.assertEqual(help_run.returncode, 0, self.output(help_run))
                self.assertIn(f"usage: {command} ", help_run.stdout)
                version_run = self.run_cli(command, "--version")
                self.assertEqual(version_run.returncode, 0, self.output(version_run))
                self.assertTrue(version_run.stdout.strip())

    def test_empty_directory_succeeds(self):
        for command in ("auto-latexmk", "auto-latexmk-clean", "auto-latexmk-rebuild"):
            run = self.run_cli(command)
            self.assertEqual(run.returncode, 0, self.output(run))
            self.assertIn("No LaTeX main documents", self.output(run))


@unittest.skipUnless(
    LATEXMK_AVAILABLE and PDFLATEX_AVAILABLE,
    "requires latexmk and pdflatex on PATH",
)
class PdfLatexCliIntegrationTests(CliIntegrationTestCase):
    def test_compile_clean_and_rebuild_lifecycle(self):
        self.copy_project("english-project")
        compiled = self.run_cli("auto-latexmk", "-pdf", "--no-engine-cache")
        self.assertEqual(compiled.returncode, 0, self.output(compiled))
        self.assertIn("2 succeeded, 0 failed", self.output(compiled))
        for directory in ("notes", "report"):
            self.assertTrue((self.root / directory / "main.pdf").is_file())
            self.assertTrue((self.root / directory / ".aux/main.log").is_file())
            self.assertTrue(
                (self.root / directory / ".aux/main.auto-latexmk.log").is_file()
            )
        self.assertFalse((self.root / "ignored/draft.pdf").exists())

        cleaned = self.run_cli("auto-latexmk-clean")
        self.assertEqual(cleaned.returncode, 0, self.output(cleaned))
        self.assertIn("2 succeeded", self.output(cleaned))
        for directory in ("notes", "report"):
            self.assertTrue((self.root / directory / "main.pdf").is_file())
            self.assertFalse((self.root / directory / ".aux/main.aux").exists())

        rebuilt = self.run_cli("auto-latexmk-rebuild", "-pdf", "--no-engine-cache")
        self.assertEqual(rebuilt.returncode, 0, self.output(rebuilt))
        self.assertIn("2 succeeded", self.output(rebuilt))
        for directory in ("notes", "report"):
            self.assertTrue((self.root / directory / "main.pdf").is_file())
            self.assertTrue((self.root / directory / ".aux/main.aux").is_file())
            log = (self.root / directory / ".aux/main.auto-latexmk.log").read_text(
                encoding="utf-8"
            )
            self.assertLess(log.index("Step: clean"), log.index("Step: compile"))
            self.assertIn("Command:", log)
            self.assertIn("Working directory:", log)

    def test_failed_compile_shows_current_diagnostics_and_saves_full_output(self):
        self.copy_project("failing-project")
        completed = self.run_cli("auto-latexmk", "-pdf", "--no-engine-cache")
        output = self.output(completed)
        self.assertEqual(completed.returncode, 1, output)
        self.assertIn("0 succeeded, 1 failed", output)
        self.assertIn("Engine: pdflatex", output)
        self.assertIn("Undefined control sequence", output)
        self.assertIn("TeX log:", output)
        self.assertIn("Full output:", output)
        saved = self.root / "course/chapter/.aux/main.auto-latexmk.log"
        self.assertTrue(saved.is_file())
        self.assertIn("Undefined control sequence", saved.read_text(encoding="utf-8"))

    def test_failed_rebuild_preserves_existing_pdf(self):
        self.copy_project("english-project")
        main_file = self.root / "notes/main.tex"
        compiled = self.run_cli(
            "auto-latexmk", "-pdf", "--no-engine-cache", str(main_file)
        )
        self.assertEqual(compiled.returncode, 0, self.output(compiled))
        pdf = main_file.with_suffix(".pdf")
        main_file.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\undefinedcommand\n\\end{document}\n",
            encoding="utf-8",
        )
        rebuilt = self.run_cli(
            "auto-latexmk-rebuild", "-pdf", "--no-engine-cache", str(main_file)
        )
        self.assertEqual(rebuilt.returncode, 1, self.output(rebuilt))
        self.assertTrue(pdf.is_file())
        self.assertIn("Undefined control sequence", self.output(rebuilt))

    def test_verbose_and_single_file_show_command_and_compiler_output(self):
        self.copy_project("english-project")
        completed = self.run_cli(
            "auto-latexmk",
            "--engine",
            "pdflatex",
            "--no-engine-cache",
            "--verbose",
            str(self.root / "notes/main.tex"),
        )
        output = self.output(completed)
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("1 succeeded", output)
        self.assertIn("Command: latexmk", output)
        self.assertIn("This is pdfTeX", output)
        self.assertFalse((self.root / "report/main.pdf").exists())

    def test_no_gitignore_includes_broken_draft_and_finishes_other_documents(self):
        self.copy_project("english-project")
        completed = self.run_cli(
            "auto-latexmk",
            "-pdf",
            "--no-engine-cache",
            "--no-gitignore",
        )
        output = self.output(completed)
        self.assertEqual(completed.returncode, 1, output)
        self.assertIn("2 succeeded, 1 failed", output)
        self.assertTrue((self.root / "notes/main.pdf").is_file())
        self.assertTrue((self.root / "report/main.pdf").is_file())


@unittest.skipUnless(
    LATEXMK_AVAILABLE and XELATEX_AVAILABLE and CTEX_AVAILABLE,
    "requires latexmk, xelatex, and ctexart.cls",
)
class XeLatexCliIntegrationTests(CliIntegrationTestCase):
    def test_chinese_document_uses_directive_and_compiles(self):
        self.copy_project("chinese-project")
        completed = self.run_cli("auto-latexmk", "--no-engine-cache")
        output = self.output(completed)
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("1 succeeded", output)
        self.assertTrue((self.root / "thesis/main.pdf").is_file())
        log = (self.root / "thesis/.aux/main.auto-latexmk.log").read_text(
            encoding="utf-8"
        )
        self.assertIn("Engine: xelatex (% !TEX program = xelatex)", log)
        self.assertIn("-pdfxe", log)


if __name__ == "__main__":
    unittest.main()
