# auto-latexmk

A one-shot command-line tool that recursively finds and compiles main LaTeX
files with automatic engine selection.

## Install

```bash
uv tool install git+ssh://git@github.com/fenglielie/auto-latexmk.git@main

# or https
uv tool install git+https://github.com/fenglielie/auto-latexmk.git@main
```

For local development:

```bash
uv tool install -e .
```

## Usage

```text
auto-latexmk [-h] [--output-mode {progressbar,list,json}]
             [-pdfxe | -pdf | -pdflua] [--debug-log FILE]
             [--no-color] [--dry-run]
             [-c] [--no-compile] [-j N]
             [--timeout SECONDS] [-x PATTERN]
             [--no-gitignore] [--no-cache-pref] [-v] [PATH]
```

```bash
auto-latexmk                      # scan the current directory
auto-latexmk main.tex             # compile one file
auto-latexmk -c .                 # clean .aux/ directories, then compile
auto-latexmk -c --no-compile .    # clean only
auto-latexmk --dry-run            # list tasks without compiling or cleaning
auto-latexmk -x 'ref*' -x '*-notes.tex'
auto-latexmk --no-gitignore
auto-latexmk -pdf .               # use pdfLaTeX
auto-latexmk -j 2 .               # use two concurrent jobs
auto-latexmk -j 1 .               # compile sequentially
auto-latexmk --timeout 300 .
auto-latexmk --output-mode list .
auto-latexmk --output-mode json . > result.json
auto-latexmk --debug-log debug.log .
```

Key options:

| Option                            | Description                                                                                    |
| --------------------------------- | ---------------------------------------------------------------------------------------------- |
| `PATH`                            | Directory to scan recursively, or one `.tex` file; defaults to the current directory           |
| `-h`, `--help`                    | Show help and exit                                                                             |
| `--output-mode MODE`              | Console output: `progressbar`, `list`, or `json`; defaults to `progressbar`                    |
| `-pdfxe` / `-pdf` / `-pdflua`     | Use XeLaTeX, pdfLaTeX, or LuaLaTeX                                                             |
| `--debug-log FILE`                | Write detailed diagnostics and compiler output to a file                                       |
| `--no-color`                      | Disable colored output                                                                         |
| `--dry-run`                       | List tasks without compiling or cleaning                                                       |
| `-c`, `--pre-clean`               | Clean `.aux/` directories before compiling                                                     |
| `--no-compile`                    | Skip compilation; useful together with `--pre-clean`                                           |
| `-j N`, `--jobs N`                | Concurrent jobs; use `1` for sequential compilation; defaults to `min(4, CPU count)`           |
| `--timeout SECONDS`               | Per-file timeout; defaults to `180`                                                            |
| `-x PATTERN`, `--exclude PATTERN` | Skip matching directories or `.tex` files; repeat for multiple fnmatch patterns                |
| `--no-gitignore`                  | Do not apply `.gitignore` rules during directory scans                                         |
| `--no-cache-pref`                 | Disable compiler preference cache                                                              |
| `-v`, `--version`                 | Print version and exit                                                                         |

The engine is determined per file in this order: `% !TEX` shebang on line 1,
then `ctex` package detection, then the CLI engine flag, then the compiler
preference cache, and finally `xelatex`.

After a successful compilation, `auto-latexmk` records the compiler used for
each main `.tex` file in
`~/.cache/auto-latexmk/compiler-preferences.json`. Use `--no-cache-pref` to
disable this behavior.

`progressbar` and `list` output are written to stderr. `json` writes only JSON
to stdout. Both human-readable modes list failed tasks with their error
summaries and LaTeX log paths after all tasks finish. Debug logs are independent
of the console output mode.

Exit status is `0` when all tasks succeed, `1` when any compilation fails, `2`
for CLI usage errors, and `130` when interrupted.

## Develop

```bash
uv sync
uv run python -m unittest discover -s tests
```

The unit tests do not require a local LaTeX toolchain. Running actual
compilations requires `latexmk` and a LaTeX distribution such as TeX Live.
