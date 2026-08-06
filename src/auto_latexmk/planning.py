import fnmatch
import os

from pathspec import GitIgnoreSpec

from auto_latexmk.cache import CompilerPrefCache, canonical_tex_path
from auto_latexmk.models import (
    CommandSpec,
    LatexTask,
    StepType,
    TaskPlan,
    TaskType,
)
from auto_latexmk.runtime import AutoLatexmkError, logger


def _matches_patterns(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern.rstrip("/\\")) for pattern in patterns)


def strip_latex_comment(line: str) -> str:
    for index, character in enumerate(line):
        if character != "%":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return line[:index]
    return line


def is_main_tex_file(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as source:
            return any("\\documentclass" in strip_latex_comment(line) for line in source)
    except (OSError, UnicodeError) as error:
        logger.error("Error reading %s: %s", path, error)
        return False


def get_tex_engine(
    tex_file: str,
    *,
    requested_engine: str,
    pref_cache: CompilerPrefCache | None,
) -> tuple[str, str]:
    if requested_engine != "auto":
        return requested_engine, "command-line option"

    try:
        with open(tex_file, "r", encoding="utf-8") as source:
            first_line = source.readline().strip()
        if first_line.startswith("% !TEX"):
            for engine in ("xelatex", "pdflatex", "lualatex"):
                if engine in first_line:
                    return engine, first_line
    except (OSError, UnicodeError) as error:
        logger.error("Error reading %s: %s", tex_file, error)

    if pref_cache is not None:
        cached = pref_cache.get(tex_file)
        if cached is not None:
            return cached, "engine cache"
    return "xelatex", "fallback"


def build_compile_command(tex_file: str, subdir: str, engine: str) -> CommandSpec:
    mode_flag = {
        "pdflatex": "pdf",
        "xelatex": "pdfxe",
        "lualatex": "pdflua",
    }[engine]
    return CommandSpec(
        step=StepType.COMPILE,
        cwd=subdir,
        argv=(
            "latexmk",
            "-file-line-error",
            "-halt-on-error",
            "-interaction=nonstopmode",
            "-synctex=1",
            f"-{mode_flag}",
            "-auxdir=.aux",
            "-outdir=.",
            f"./{os.path.basename(tex_file)}",
        ),
    )


def build_clean_command(tex_file: str, subdir: str) -> CommandSpec:
    return CommandSpec(
        step=StepType.CLEAN,
        cwd=subdir,
        argv=(
            "latexmk",
            "-c",
            "-auxdir=.aux",
            "-outdir=.",
            f"./{os.path.basename(tex_file)}",
        ),
    )


def build_task(
    *,
    index: int,
    tex_file: str,
    task_type: TaskType,
    requested_engine: str,
    pref_cache: CompilerPrefCache | None,
) -> LatexTask:
    tex_file = canonical_tex_path(tex_file)
    subdir = os.path.dirname(tex_file).replace("\\", "/")
    engine = None
    engine_reason = None
    commands = []

    if task_type in (TaskType.CLEAN, TaskType.CLEAN_COMPILE):
        commands.append(build_clean_command(tex_file, subdir))
    if task_type in (TaskType.COMPILE, TaskType.CLEAN_COMPILE):
        engine, engine_reason = get_tex_engine(
            tex_file,
            requested_engine=requested_engine,
            pref_cache=pref_cache,
        )
        commands.append(build_compile_command(tex_file, subdir, engine))

    return LatexTask(
        index=index,
        task_type=task_type,
        tex_file=tex_file,
        engine=engine,
        engine_reason=engine_reason,
        commands=tuple(commands),
    )


def _discover_tex_files(
    path: str,
    *,
    exclude_patterns: list[str],
    use_gitignore: bool,
) -> list[str]:
    discovered = []
    ignore_scopes = {}

    for subdir, dirs, files in os.walk(path, topdown=True):
        parent_scopes = ignore_scopes.get(os.path.dirname(subdir), [])
        scopes = list(parent_scopes)
        gitignore_path = os.path.join(subdir, ".gitignore")
        if use_gitignore and os.path.isfile(gitignore_path):
            try:
                with open(gitignore_path, "r", encoding="utf-8") as rules:
                    scopes.append((subdir, GitIgnoreSpec.from_lines(rules)))
            except OSError as error:
                logger.warning("Could not read %s: %s", gitignore_path, error)
        ignore_scopes[subdir] = scopes

        def is_gitignored(candidate, *, is_dir=False, _scopes=tuple(scopes)):
            ignored = False
            for scope_dir, spec in _scopes:
                relative = os.path.relpath(candidate, scope_dir).replace("\\", "/")
                result = spec.check_file(f"{relative}/" if is_dir else relative)
                if result.include is not None:
                    ignored = result.include
            return ignored

        dirs[:] = sorted(
            [
                name
                for name in dirs
                if not name.startswith(".")
                and name not in {".git", ".aux"}
                and not _matches_patterns(name, exclude_patterns)
                and not (
                    use_gitignore
                    and is_gitignored(os.path.join(subdir, name), is_dir=True)
                )
            ],
            key=lambda name: (name.casefold(), name),
        )

        for name in sorted(files, key=lambda item: (item.casefold(), item)):
            if not name.endswith(".tex") or _matches_patterns(name, exclude_patterns):
                continue
            candidate = os.path.join(subdir, name)
            if use_gitignore and is_gitignored(candidate):
                continue
            candidate = canonical_tex_path(candidate)
            if is_main_tex_file(candidate):
                discovered.append(candidate)
    return discovered


def create_task_plan(
    path: str,
    *,
    task_type: TaskType,
    requested_engine: str,
    exclude_patterns: list[str],
    use_gitignore: bool,
    pref_cache: CompilerPrefCache | None,
) -> TaskPlan:
    input_path = canonical_tex_path(path)
    if os.path.isfile(input_path):
        if not input_path.endswith(".tex"):
            raise AutoLatexmkError(f"{input_path} is not a .tex file.")
        if not is_main_tex_file(input_path):
            raise AutoLatexmkError(f"{input_path} is not a LaTeX main file.")
        root_dir = os.path.dirname(input_path)
        tex_files = [input_path]
    elif os.path.isdir(input_path):
        root_dir = input_path
        tex_files = _discover_tex_files(
            input_path,
            exclude_patterns=exclude_patterns,
            use_gitignore=use_gitignore,
        )
    else:
        raise AutoLatexmkError(f"{input_path} does not exist.")

    tasks = tuple(
        build_task(
            index=index,
            tex_file=tex_file,
            task_type=task_type,
            requested_engine=requested_engine,
            pref_cache=pref_cache,
        )
        for index, tex_file in enumerate(tex_files, start=1)
    )
    return TaskPlan(root_dir=root_dir, task_type=task_type, tasks=tasks)
