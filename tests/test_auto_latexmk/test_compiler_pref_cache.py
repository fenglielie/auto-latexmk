import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from auto_latexmk.auto_latexmk import (
    AutoLatexmkError,
    CompilerPrefCache,
    clean_all_aux_subdirs_if_exist,
    generate_compile_tasks_in_dir,
    generate_tasks_from_input,
    get_tex_engine,
    is_main_tex_file,
    parse_args,
)


class CLIParsingTests(unittest.TestCase):
    def test_latexmk_engine_options(self):
        cases = [
            ("-pdfxe", "xelatex"),
            ("-pdf", "pdflatex"),
            ("-pdflua", "lualatex"),
        ]
        for option, expected_engine in cases:
            with self.subTest(option=option):
                self.assertEqual(parse_args([option]).engine, expected_engine)

    def test_exclude_is_repeatable_without_consuming_path(self):
        args = parse_args(["-x", "ref*", "-x", "*-notes.tex", "project"])

        self.assertEqual(args.path, "project")
        self.assertEqual(args.exclude, ["ref*", "*-notes.tex"])


class CompilerPrefCacheTests(unittest.TestCase):
    def test_save_preserves_entries_when_cache_was_not_loaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "compiler-preferences.json")
            existing_path = os.path.join(temp_dir, "other-project", "main.tex")
            new_path = os.path.join(temp_dir, "current-project", "main.tex")
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        existing_path: {
                            "engine": "pdflatex",
                            "updated_at": 1.0,
                        }
                    },
                    f,
                )

            cache = CompilerPrefCache(cache_path)
            cache.set(new_path, "xelatex")
            cache.save()

            with open(cache_path, "r", encoding="utf-8") as f:
                saved = json.load(f)

            self.assertEqual(saved[existing_path]["engine"], "pdflatex")
            self.assertEqual(saved[new_path]["engine"], "xelatex")

    def test_load_after_set_keeps_pending_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "compiler-preferences.json")
            pending_path = os.path.join(temp_dir, "current-project", "main.tex")
            cache = CompilerPrefCache(cache_path)

            cache.set(pending_path, "xelatex")

            self.assertEqual(cache.get(pending_path), "xelatex")

    def test_concurrent_saves_merge_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "compiler-preferences.json")
            caches = [CompilerPrefCache(cache_path), CompilerPrefCache(cache_path)]
            paths = [
                os.path.join(temp_dir, "project-a", "main.tex"),
                os.path.join(temp_dir, "project-b", "main.tex"),
            ]
            caches[0].set(paths[0], "pdflatex")
            caches[1].set(paths[1], "lualatex")

            barrier = threading.Barrier(2)

            def save(cache):
                barrier.wait()
                cache.save()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(save, cache) for cache in caches]
                for future in futures:
                    future.result()

            with open(cache_path, "r", encoding="utf-8") as f:
                saved = json.load(f)

            self.assertEqual(saved[paths[0]]["engine"], "pdflatex")
            self.assertEqual(saved[paths[1]]["engine"], "lualatex")


class TexDiscoveryTests(unittest.TestCase):
    def write_tex(self, directory, name, content):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path

    def test_is_main_tex_file_requires_documentclass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            main_path = self.write_tex(
                temp_dir,
                "main.tex",
                "\\documentclass{article}\n",
            )
            fragment_path = self.write_tex(temp_dir, "fragment.tex", "Only body text")

            self.assertTrue(is_main_tex_file(main_path))
            self.assertFalse(is_main_tex_file(fragment_path))

    def test_commented_documentclass_is_not_a_main_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_path = self.write_tex(
                temp_dir,
                "commented.tex",
                "% \\documentclass{article}\nOnly body text\n",
            )

            self.assertFalse(is_main_tex_file(tex_path))

    def test_get_tex_engine_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shebang_path = self.write_tex(
                temp_dir,
                "shebang.tex",
                "% !TEX program = pdflatex\n\\documentclass{article}\n",
            )
            cached_path = self.write_tex(
                temp_dir,
                "cached.tex",
                "\\documentclass{article}\n",
            )
            fallback_path = self.write_tex(
                temp_dir,
                "fallback.tex",
                "\\documentclass{article}\n",
            )

            cache = CompilerPrefCache(os.path.join(temp_dir, "cache.json"))
            cache.set(shebang_path, "xelatex")
            cache.set(cached_path, "lualatex")

            self.assertEqual(
                get_tex_engine(shebang_path, default_engine=None, pref_cache=cache)[0],
                "pdflatex",
            )
            self.assertEqual(
                get_tex_engine(
                    shebang_path,
                    default_engine="lualatex",
                    pref_cache=cache,
                )[0],
                "lualatex",
            )
            self.assertEqual(
                get_tex_engine(cached_path, default_engine=None, pref_cache=cache)[0],
                "lualatex",
            )
            self.assertEqual(
                get_tex_engine(
                    cached_path, default_engine="pdflatex", pref_cache=cache
                )[0],
                "pdflatex",
            )
            self.assertEqual(
                get_tex_engine(fallback_path, default_engine=None, pref_cache=None)[0],
                "xelatex",
            )

    def test_ctex_is_not_an_engine_signal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_path = self.write_tex(
                temp_dir,
                "ctex.tex",
                "\\documentclass{ctexart}\n",
            )
            cache = CompilerPrefCache(os.path.join(temp_dir, "cache.json"))
            cache.set(tex_path, "lualatex")

            self.assertEqual(
                get_tex_engine(
                    tex_path,
                    default_engine=None,
                    pref_cache=cache,
                )[0],
                "lualatex",
            )

    def test_clean_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            aux_dir = os.path.join(temp_dir, ".aux")
            os.mkdir(aux_dir)

            with (
                patch(
                    "auto_latexmk.auto_latexmk.shutil.rmtree",
                    side_effect=OSError("access denied"),
                ),
                self.assertRaisesRegex(
                    AutoLatexmkError,
                    "Failed to delete",
                ),
            ):
                clean_all_aux_subdirs_if_exist(temp_dir, exclude_patterns=[])

    def test_generate_tasks_respects_exclude_patterns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            keep_path = self.write_tex(
                temp_dir, "keep.tex", "\\documentclass{article}\n"
            )
            self.write_tex(temp_dir, "draft-notes.tex", "\\documentclass{article}\n")
            subdir = os.path.join(temp_dir, "ignored")
            os.mkdir(subdir)
            self.write_tex(subdir, "main.tex", "\\documentclass{article}\n")

            tasks = generate_compile_tasks_in_dir(
                temp_dir,
                default_engine="pdflatex",
                timeout=10,
                exclude_patterns=["ignored", "draft-*"],
                pref_cache=None,
            )

            keep_path_normalized = os.path.abspath(keep_path).replace("\\", "/")
            self.assertEqual(
                [task.tex_file for task in tasks], [keep_path_normalized]
            )

    def test_generate_tasks_respects_gitignore_and_negation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ignored = self.write_tex(
                temp_dir, "draft.tex", "\\documentclass{article}\n"
            )
            kept = self.write_tex(temp_dir, "keep.tex", "\\documentclass{article}\n")
            ignored_dir = os.path.join(temp_dir, "build")
            os.mkdir(ignored_dir)
            self.write_tex(ignored_dir, "main.tex", "\\documentclass{article}\n")
            with open(os.path.join(temp_dir, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("*.tex\n!keep.tex\nbuild/\n")

            kwargs = {
                "default_engine": "pdflatex",
                "timeout": 10,
                "exclude_patterns": [],
                "pref_cache": None,
            }
            tasks = generate_compile_tasks_in_dir(temp_dir, **kwargs)
            self.assertEqual(
                [task.tex_file for task in tasks],
                [os.path.abspath(kept).replace("\\", "/")],
            )

            tasks_without_gitignore = generate_compile_tasks_in_dir(
                temp_dir, use_gitignore=False, **kwargs
            )
            self.assertEqual(len(tasks_without_gitignore), 3)
            self.assertIn(
                os.path.abspath(ignored).replace("\\", "/"),
                [task.tex_file for task in tasks_without_gitignore],
            )

    def test_explicit_tex_file_bypasses_gitignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tex_file = self.write_tex(
                temp_dir, "ignored.tex", "\\documentclass{article}\n"
            )
            with open(os.path.join(temp_dir, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("ignored.tex\n")

            tasks = generate_tasks_from_input(
                tex_file,
                default_engine="pdflatex",
                timeout=10,
                exclude_patterns=[],
                pref_cache=None,
            )
            self.assertEqual(
                [task.tex_file for task in tasks],
                [os.path.abspath(tex_file).replace("\\", "/")],
            )


if __name__ == "__main__":
    unittest.main()
