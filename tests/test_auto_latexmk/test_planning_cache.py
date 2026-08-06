import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from auto_latexmk.cache import CompilerPrefCache, canonical_tex_path
from auto_latexmk.models import StepType, TaskType
from auto_latexmk.planning import create_task_plan, get_tex_engine, is_main_tex_file


class PlanningTests(unittest.TestCase):
    def write_tex(self, directory, name, content="\\documentclass{article}\n"):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        return path

    def test_main_document_detection_ignores_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            main = self.write_tex(directory, "main.tex")
            fragment = self.write_tex(
                directory, "fragment.tex", "% \\documentclass{article}\ntext\n"
            )
            self.assertTrue(is_main_tex_file(main))
            self.assertFalse(is_main_tex_file(fragment))

    def test_compile_clean_and_clean_compile_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            tex_file = self.write_tex(directory, "main.tex")
            expected = {
                TaskType.COMPILE: [StepType.COMPILE],
                TaskType.CLEAN: [StepType.CLEAN],
                TaskType.CLEAN_COMPILE: [StepType.CLEAN, StepType.COMPILE],
            }
            for task_type, steps in expected.items():
                with self.subTest(task_type=task_type):
                    plan = create_task_plan(
                        tex_file,
                        task_type=task_type,
                        requested_engine="auto",
                        exclude_patterns=[],
                        use_gitignore=True,
                        pref_cache=None,
                    )
                    task = plan.tasks[0]
                    self.assertEqual([command.step for command in task.commands], steps)
                    self.assertEqual(task.engine, None if task_type is TaskType.CLEAN else "xelatex")
                    self.assertTrue(all(command.argv[0] == "latexmk" for command in task.commands))
                    self.assertTrue(all(command.cwd == canonical_tex_path(directory) for command in task.commands))

    def test_engine_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            tex_file = self.write_tex(
                directory,
                "main.tex",
                "% !TEX program = pdflatex\n\\documentclass{article}\n",
            )
            cache = CompilerPrefCache(os.path.join(directory, "cache.json"))
            cache.set(tex_file, "lualatex")
            self.assertEqual(
                get_tex_engine(tex_file, requested_engine="auto", pref_cache=cache)[0],
                "pdflatex",
            )
            self.assertEqual(
                get_tex_engine(tex_file, requested_engine="xelatex", pref_cache=cache)[0],
                "xelatex",
            )

    def test_directory_scan_respects_exclude_and_gitignore(self):
        with tempfile.TemporaryDirectory() as directory:
            keep = self.write_tex(directory, "keep.tex")
            self.write_tex(directory, "draft.tex")
            ignored_dir = os.path.join(directory, "generated")
            os.mkdir(ignored_dir)
            self.write_tex(ignored_dir, "main.tex")
            with open(os.path.join(directory, ".gitignore"), "w", encoding="utf-8") as output:
                output.write("draft.tex\n")
            plan = create_task_plan(
                directory,
                task_type=TaskType.COMPILE,
                requested_engine="auto",
                exclude_patterns=["generated"],
                use_gitignore=True,
                pref_cache=None,
            )
            self.assertEqual([task.tex_file for task in plan.tasks], [canonical_tex_path(keep)])

    def test_explicit_file_bypasses_gitignore(self):
        with tempfile.TemporaryDirectory() as directory:
            tex_file = self.write_tex(directory, "main.tex")
            with open(os.path.join(directory, ".gitignore"), "w", encoding="utf-8") as output:
                output.write("main.tex\n")
            plan = create_task_plan(
                tex_file,
                task_type=TaskType.COMPILE,
                requested_engine="auto",
                exclude_patterns=[],
                use_gitignore=True,
                pref_cache=None,
            )
            self.assertEqual(len(plan.tasks), 1)

    def test_directory_scan_has_deterministic_case_insensitive_order(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_tex(directory, "zeta.tex")
            nested = os.path.join(directory, "Beta")
            os.mkdir(nested)
            self.write_tex(nested, "main.tex")
            self.write_tex(directory, "alpha.tex")

            plan = create_task_plan(
                directory,
                task_type=TaskType.COMPILE,
                requested_engine="auto",
                exclude_patterns=[],
                use_gitignore=True,
                pref_cache=None,
            )

            self.assertEqual(
                [os.path.relpath(task.tex_file, directory).replace("\\", "/") for task in plan.tasks],
                ["alpha.tex", "zeta.tex", "Beta/main.tex"],
            )


class CacheCompatibilityTests(unittest.TestCase):
    def test_existing_schema_is_reused_and_invalid_engines_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = os.path.join(directory, "compiler-preferences.json")
            tex_file = os.path.join(directory, "main.tex")
            invalid_file = os.path.join(directory, "invalid.tex")
            with open(cache_path, "w", encoding="utf-8") as output:
                json.dump(
                    {
                        canonical_tex_path(tex_file): {"engine": "pdflatex", "updated_at": 1.0},
                        canonical_tex_path(invalid_file): {"engine": "unknown", "future": True}
                    },
                    output,
                )
            cache = CompilerPrefCache(cache_path)
            self.assertEqual(cache.get(tex_file), "pdflatex")
            self.assertIsNone(cache.get(invalid_file))

    def test_concurrent_saves_merge_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = os.path.join(directory, "cache.json")
            caches = [CompilerPrefCache(cache_path), CompilerPrefCache(cache_path)]
            paths = [os.path.join(directory, "a.tex"), os.path.join(directory, "b.tex")]
            caches[0].set(paths[0], "xelatex")
            caches[1].set(paths[1], "lualatex")
            barrier = threading.Barrier(2)

            def save(cache):
                barrier.wait()
                cache.save()

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(save, caches))
            with open(cache_path, encoding="utf-8") as cache_file:
                data = json.load(cache_file)
            self.assertEqual(len(data), 2)


if __name__ == "__main__":
    unittest.main()
