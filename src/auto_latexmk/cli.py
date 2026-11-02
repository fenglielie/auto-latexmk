import argparse
import logging
import os
import sys
import time

from auto_latexmk import __version__
from auto_latexmk.cache import CompilerPrefCache
from auto_latexmk.execution import (
    TaskExecutionInterrupted,
    run_tasks,
    terminate_active_processes,
)
from auto_latexmk.models import RunSummary, TaskType
from auto_latexmk.planning import create_task_plan
from auto_latexmk.reporting import Reporter
from auto_latexmk.runtime import AutoLatexmkError, logger


def parse_args(argv=None, *, task_type=TaskType.COMPILE):
    programs = {
        TaskType.COMPILE: ("auto-latexmk", "Compile LaTeX main documents."),
        TaskType.CLEAN: (
            "auto-latexmk-clean",
            "Clean LaTeX auxiliary files while preserving final PDFs.",
        ),
        TaskType.REBUILD: (
            "auto-latexmk-rebuild",
            "Clean auxiliary files, then compile LaTeX main documents.",
        ),
    }
    prog, description = programs[task_type]
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  {prog}\n"
            f"  {prog} main.tex\n"
            f"  {prog} notes -x '*-draft.tex'\n"
            f"  {prog} main.tex --verbose\n\n"
            "Full command output is saved under .aux/ for each document.\n"
            "Use Ctrl+C to stop; there is no compilation time limit."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=os.getcwd(),
        metavar="PATH",
        help="Directory to scan recursively, or one main .tex file (default: current directory).",
    )
    if task_type is not TaskType.CLEAN:
        engines = parser.add_mutually_exclusive_group()
        engines.add_argument(
            "--engine",
            choices=["auto", "xelatex", "pdflatex", "lualatex"],
            help="Engine: auto uses the first-line %% !TEX directive, then cache, then xelatex.",
        )
        for flag, engine in (
            ("-pdfxe", "xelatex"),
            ("-pdf", "pdflatex"),
            ("-pdflua", "lualatex"),
        ):
            engines.add_argument(
                flag,
                action="store_const",
                const=engine,
                dest="engine",
                help=f"Equivalent to --engine {engine}.",
            )
        parser.add_argument(
            "-j",
            "--jobs",
            type=int,
            default=min(4, os.cpu_count() or 1),
            metavar="N",
            help="Maximum concurrent documents (default: min(4, CPU count)).",
        )
        parser.add_argument(
            "--no-engine-cache",
            action="store_true",
            help="Do not read or update successful-engine preferences.",
        )
    else:
        parser.set_defaults(engine=None, jobs=1, no_engine_cache=True)
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Exclude a matching directory or .tex basename; repeatable.",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_false",
        dest="use_gitignore",
        help="Do not apply .gitignore rules during directory scanning.",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable terminal colors."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show commands and full output, grouped by document after each finishes.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    args.engine = args.engine or "auto"
    args.exclude = [pattern.rstrip("/\\") for pattern in args.exclude]
    return args


def _run(task_type, argv=None):
    args = parse_args(argv, task_type=task_type)
    reporter = Reporter(
        task_type=task_type, color=not args.no_color, verbose=args.verbose
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        pref_cache = None if args.no_engine_cache else CompilerPrefCache()
        plan = create_task_plan(
            args.path,
            task_type=task_type,
            requested_engine=args.engine,
            exclude_patterns=args.exclude,
            use_gitignore=args.use_gitignore,
            pref_cache=pref_cache,
        )
        run_start = time.monotonic()
        reporter.start(plan.tasks)
        interrupted = False
        try:
            results = run_tasks(plan.tasks, jobs=args.jobs, reporter=reporter)
        except TaskExecutionInterrupted as error:
            results = error.results
            interrupted = True
        if pref_cache is not None:
            for result in results:
                if result.success and result.task.engine is not None:
                    pref_cache.set(result.task.tex_file, result.task.engine)
            pref_cache.save()
        summary = RunSummary(time.monotonic() - run_start, results)
        reporter.finish(summary)
        if interrupted or summary.cancelled:
            return 130
        return 1 if summary.failed else 0
    except AutoLatexmkError as error:
        reporter.fatal_error(str(error))
        return 1
    except KeyboardInterrupt:
        terminate_active_processes()
        reporter.fatal_error("Interrupted.")
        return 130
    except Exception as error:  # noqa: BLE001 - CLI boundary
        reporter.fatal_error(f"Unexpected error: {error}")
        if args.verbose:
            logger.exception("Unexpected error")
        return 1
    finally:
        reporter.close()
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


def main(argv=None):
    return _run(TaskType.COMPILE, argv)


def clean_main(argv=None):
    return _run(TaskType.CLEAN, argv)


def rebuild_main(argv=None):
    return _run(TaskType.REBUILD, argv)
