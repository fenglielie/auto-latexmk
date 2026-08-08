import argparse
import logging
import os
import sys
import time

from auto_latexmk import __version__
from auto_latexmk.actions import export_plan, preview_plan
from auto_latexmk.cache import CompilerPrefCache
from auto_latexmk.execution import (
    TaskExecutionInterrupted,
    run_tasks,
    terminate_active_processes,
)
from auto_latexmk.models import Action, RunSummary, TaskType
from auto_latexmk.planning import create_task_plan
from auto_latexmk.reporting import Reporter, create_reporter
from auto_latexmk.runtime import AutoLatexmkError, logger


def configure_debug_logging(path):
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    if path is None:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        return
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="auto-latexmk",
        description=(
            "Discover LaTeX main documents, construct latexmk tasks, and run, "
            "preview, or export the resulting plan."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Task/action model:
  --task chooses the ordered steps constructed for every main document.
  --action chooses whether that completed plan is run, previewed, or exported.
  With no options, the current directory is scanned and compile tasks are run.

Output channels:
  progressbar and ordinary diagnostics -> stderr
  list, preview, JSON, and exported scripts -> stdout

Scope rules:
  --output-mode, --jobs, and --timeout are valid only with --action run.
  --export-format is valid only with --action export.
  Engine options and --no-engine-cache are invalid with --task clean.
  --no-color is invalid with JSON output or command export.

Examples:
  auto-latexmk
  auto-latexmk --task clean
  auto-latexmk --task clean-compile --action preview notes
  auto-latexmk --engine pdflatex --jobs 2 main.tex
  auto-latexmk --output-mode json > result.json
  auto-latexmk --action export --no-engine-cache > commands.txt
""",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=os.getcwd(),
        metavar="PATH",
        help=(
            "Directory to scan recursively, or one main .tex file. "
            "(default: current directory)"
        ),
    )
    parser.add_argument(
        "--task",
        choices=[item.value for item in TaskType],
        default=TaskType.COMPILE.value,
        help=(
            "Steps to construct per document: compile, latexmk -c clean, or "
            "clean followed by compile. (default: compile)"
        ),
    )
    parser.add_argument(
        "--action",
        choices=[item.value for item in Action],
        default=Action.RUN.value,
        help=(
            "Consume the completed plan by executing it, displaying it, or "
            "exporting a platform-native script. (default: run)"
        ),
    )
    parser.add_argument(
        "--export-format",
        choices=["auto", "pwsh", "bash"],
        default="auto",
        help=(
            "Script format for export; valid only with --action export. "
            "auto selects PowerShell on Windows and sh on POSIX. "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--output-mode",
        choices=["progressbar", "list", "json"],
        default=None,
        help=(
            "Execution-result format; valid only with --action run. "
            "(default: progressbar)"
        ),
    )

    engine_group = parser.add_mutually_exclusive_group()
    engine_group.add_argument(
        "--engine",
        choices=["auto", "xelatex", "pdflatex", "lualatex"],
        default=None,
        help=(
            "Compile engine. auto checks the first-line %% !TEX directive, then "
            "the successful-engine cache, then falls back to xelatex. "
            "(default: auto)"
        ),
    )
    engine_group.add_argument(
        "-pdfxe",
        action="store_const",
        const="xelatex",
        dest="engine",
        help="Equivalent to --engine xelatex.",
    )
    engine_group.add_argument(
        "-pdf",
        action="store_const",
        const="pdflatex",
        dest="engine",
        help="Equivalent to --engine pdflatex.",
    )
    engine_group.add_argument(
        "-pdflua",
        action="store_const",
        const="lualatex",
        dest="engine",
        help="Equivalent to --engine lualatex.",
    )

    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum concurrent document tasks; valid only with --action run. "
            "(default: min(4, CPU count))"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Timeout for each latexmk process; valid only with --action run. "
            "(default: 180)"
        ),
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Exclude a matching directory or .tex basename using fnmatch "
            "syntax; repeatable."
        ),
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_false",
        dest="use_gitignore",
        help="Do not apply nested .gitignore rules during directory discovery.",
    )
    parser.set_defaults(use_gitignore=True)
    parser.add_argument(
        "--no-engine-cache",
        action="store_true",
        help=(
            "Do not read or write successful-engine preferences; valid for "
            "compile and clean-compile tasks."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help=(
            "Disable ANSI color in human-readable output; not applicable to "
            "JSON or export."
        ),
    )
    parser.add_argument(
        "--debug-log",
        metavar="FILE",
        help=(
            "Overwrite FILE with discovery details, constructed commands, "
            "compiler output, and exceptions."
        ),
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
        help="Show the installed version and exit.",
    )

    args = parser.parse_args(argv)
    args.task = TaskType(args.task)
    args.action = Action(args.action)

    if args.task is TaskType.CLEAN and args.engine is not None:
        parser.error("--task clean does not accept an engine option")
    if args.task is TaskType.CLEAN and args.no_engine_cache:
        parser.error("--task clean does not use the engine cache")
    if args.action is not Action.RUN:
        if args.output_mode is not None:
            parser.error("--output-mode is valid only with --action run")
        if args.jobs is not None:
            parser.error("--jobs is valid only with --action run")
        if args.timeout is not None:
            parser.error("--timeout is valid only with --action run")
    if args.action is not Action.EXPORT and args.export_format != "auto":
        parser.error("--export-format is valid only with --action export")
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.timeout is not None and args.timeout < 1:
        parser.error("--timeout must be at least 1")

    if args.action is Action.RUN:
        args.output_mode = args.output_mode or "progressbar"
        if args.output_mode == "json" and args.no_color:
            parser.error("--no-color is not applicable to JSON output")
        args.jobs = args.jobs or min(4, os.cpu_count() or 1)
        args.timeout = args.timeout or 180
    elif args.action is Action.EXPORT and args.no_color:
        parser.error("--no-color is not applicable to command export")

    args.engine = args.engine or "auto"
    args.exclude = [pattern.rstrip("/\\") for pattern in args.exclude]
    return args


def main():
    reporter: Reporter | None = None
    args = None
    try:
        args = parse_args()
        if args.action is Action.RUN:
            reporter = create_reporter(
                args.output_mode,
                task_type=args.task,
                color=not args.no_color,
            )
        try:
            configure_debug_logging(args.debug_log)
        except OSError as error:
            message = f"Failed to create debug log: {error}"
            if reporter is not None:
                reporter.fatal_error(message)
            else:
                print(f"ERROR: {message}", file=sys.stderr)
            return 1

        pref_cache = (
            None
            if args.task is TaskType.CLEAN or args.no_engine_cache
            else CompilerPrefCache()
        )
        plan = create_task_plan(
            args.path,
            task_type=args.task,
            requested_engine=args.engine,
            exclude_patterns=args.exclude,
            use_gitignore=args.use_gitignore,
            pref_cache=pref_cache,
        )
        logger.debug("Created %d %s task(s)", len(plan.tasks), args.task.value)

        if args.action is Action.PREVIEW:
            preview_plan(plan)
            return 0
        if args.action is Action.EXPORT:
            platform_map = {"auto": None, "pwsh": "windows", "bash": "posix"}
            export_plan(plan, platform=platform_map[args.export_format])
            return 0

        assert reporter is not None
        run_start = time.monotonic()
        reporter.start(plan.tasks)
        interrupted = False
        try:
            results = run_tasks(
                plan.tasks,
                jobs=args.jobs,
                timeout=args.timeout,
                reporter=reporter,
            )
        except TaskExecutionInterrupted as error:
            results = error.results
            interrupted = True
        if pref_cache is not None:
            for result in results:
                if result.success and result.task.engine is not None:
                    pref_cache.set(result.task.tex_file, result.task.engine)
            pref_cache.save()

        failed = any(result.status == "failed" for result in results)
        cancelled = any(result.status == "cancelled" for result in results)
        status = (
            "interrupted"
            if interrupted or cancelled
            else "failed"
            if failed
            else "succeeded"
        )
        summary = RunSummary(
            task_type=args.task,
            status=status,
            elapsed_time=time.monotonic() - run_start,
            results=results,
        )
        reporter.finish(summary)
        if interrupted:
            return 130
        return 1 if failed or cancelled else 0
    except AutoLatexmkError as error:
        logger.error("%s", error)
        if reporter is not None:
            reporter.fatal_error(str(error))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        terminate_active_processes()
        if reporter is not None:
            reporter.interrupted()
        else:
            print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary reports unexpected failures
        logger.exception("Unexpected error")
        if reporter is not None:
            reporter.fatal_error(str(error))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    finally:
        if reporter is not None:
            reporter.close()
        configure_debug_logging(None)
