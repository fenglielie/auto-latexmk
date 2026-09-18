# auto-latexmk

`auto-latexmk` discovers LaTeX main documents, constructs explicit latexmk tasks, and then runs, previews, or exports those tasks.

The default remains deliberately simple:

```bash
auto-latexmk
```

This scans the current directory and compiles every discovered main document.

## ✨ Features

- Compile one main `.tex` file or recursively process a directory.
- Construct `compile`, `clean`, or `clean-compile` tasks.
- Run tasks locally, preview the plan, or export platform-native commands.
- Select XeLaTeX, pdfLaTeX, or LuaLaTeX per document.
- Respect nested `.gitignore` files and repeatable exclude patterns.
- Run independent document tasks concurrently.
- Keep auxiliary files under `.aux/` and final output beside the source.
- Emit progress, stable result lines, or JSON schema v2.
- Preserve the existing successful-engine cache.
- Stop non-interactively on LaTeX errors and enforce per-command timeouts.

## 🧭 Mental model

Each invocation has two separate phases: **construct a task plan**, then **consume that plan**.

```text
PATH + discovery options
          │
          ▼
 discover main .tex files
          │
          ▼
 --task constructs one task per document
   ├─ compile       [compile]
   ├─ clean         [latexmk -c]
   └─ clean-compile [latexmk -c] → [compile]
          │
          ▼
 TaskPlan: documents + ordered steps + exact cwd/argv
          │
          ▼
 --action consumes the completed plan
   ├─ run      execute it now
   ├─ preview  display it without execution
   └─ export   write an executable pwsh/sh script
```

`--task` changes the commands stored in the plan. It does not decide whether they run.
For tasks containing compilation, engine selection also happens while the plan is constructed, so previewed, exported, and locally executed commands are based on the same decision.

`--action` does not rebuild or reinterpret the tasks. It only selects the plan's consumer:

| Action | Executes commands | Output | Other effects |
| --- | --- | --- | --- |
| `run` | Yes, with task-level concurrency | `progressbar`, `list`, or `json` results | Successful compilation may update the engine cache |
| `preview` | No | Human-readable plan on stdout | No cache update or generated files |
| `export` | No | Platform-native script on stdout | No cache update or generated files |

For example, `--task clean-compile --action export` first constructs an ordered clean and compile pair for every discovered document, then serializes those pairs into a script.
It does not clean or compile on the machine running `auto-latexmk`; the exported script can be executed later on a machine that only has TeX Live and the appropriate shell.

Steps inside one `clean-compile` task are always sequential: compilation starts only after that document's cleanup succeeds.
With `--action run`, different document tasks may run concurrently according to `--jobs`.

## 📦 Requirements

- Python 3.11 or newer.
- `latexmk` on `PATH`.
- A TeX distribution containing the requested engines.
- PowerShell 7 (`pwsh`) when executing commands exported on Windows, or `sh` on POSIX systems.

## 🚀 Installation

```bash
uv tool install git+https://github.com/fenglielie/auto-latexmk.git@main
```

For local development:

```bash
uv tool install -e .
```

## ⚡ Quick start

```bash
# Compile all main documents under the current directory
auto-latexmk

# Compile one document
auto-latexmk main.tex

# Clean latexmk-generated auxiliary files
auto-latexmk --task clean

# Clean auxiliary files, then compile
auto-latexmk --task clean-compile

# Inspect the task plan without changing files
auto-latexmk --action preview

# Export commands for the current platform
auto-latexmk --action export > commands.sh

# Export a clean-then-compile plan for another machine
auto-latexmk --task clean-compile --action export > rebuild.sh

# Run with two concurrent task workers
auto-latexmk --jobs 2
```

## 🎛️ Command-line interface

```text
auto-latexmk [-h]
             [--task {compile,clean,clean-compile}]
             [--action {run,preview,export}]
             [--export-format {auto,pwsh,bash}]
             [--output-mode {progressbar,list,json}]
             [--engine {auto,xelatex,pdflatex,lualatex}
               | -pdfxe | -pdf | -pdflua]
             [-j N] [--timeout SECONDS]
             [-x PATTERN] [--no-gitignore]
             [--no-engine-cache] [--no-color]
             [--debug-log FILE] [-v] [PATH]
```

### Core options

| Option | Description |
| --- | --- |
| `PATH` | Directory to scan or one main `.tex` file; defaults to the current directory |
| `--task TYPE` | Construct `compile`, `clean`, or `clean-compile` tasks; defaults to `compile` |
| `--action ACTION` | `run`, `preview`, or `export` the task plan; defaults to `run` |
| `--export-format FORMAT` | `auto`, `pwsh`, or `bash`; valid only for `export`; defaults to `auto` |
| `--output-mode MODE` | Run-result output: `progressbar`, `list`, or `json`; valid only for `run` |
| `--engine ENGINE` | `auto`, `xelatex`, `pdflatex`, or `lualatex`; defaults to `auto` |
| `-pdfxe` | Equivalent to `--engine xelatex` |
| `-pdf` | Equivalent to `--engine pdflatex` |
| `-pdflua` | Equivalent to `--engine lualatex` |
| `-j N`, `--jobs N` | Maximum concurrent tasks; valid only for `run` |
| `--timeout SECONDS` | Timeout for each latexmk process; valid only for `run` |
| `-x`, `--exclude PATTERN` | Exclude a matching basename; repeat for multiple patterns |
| `--no-gitignore` | Do not apply `.gitignore` rules during discovery |
| `--no-engine-cache` | Do not read or write successful-engine preferences |
| `--no-color` | Disable color in human output |
| `--debug-log FILE` | Overwrite `FILE` with detailed diagnostics and compiler output |
| `-v`, `--version` | Print the installed version |

Invalid cross-scope combinations are rejected.

For example, preview/export do not accept `--jobs`, `--timeout`, or `--output-mode`; `--export-format` is valid only with `--action export`; and clean tasks do not accept engine options.

The defaults are equivalent to:

```bash
auto-latexmk --task compile --action run --engine auto
```

## 🧱 Task construction

Every discovered main document becomes one independent task.

Main-file discovery scans recursively (unless a single `.tex` file is passed), honors nested `.gitignore` rules, and applies each `--exclude` pattern to the file basename.
Included/supporting `.tex` files are not treated as independent main documents.
Discovered directories and files are sorted case-insensitively, with the original spelling used as a deterministic tie-breaker.

During a directory scan:

- Hidden directories, `.git/`, and `.aux/` are not traversed.
- `compile`, `clean`, and `clean-compile` use exactly the same discovery result.
- `--exclude` uses Python `fnmatch` semantics and matches individual directory or `.tex` basenames, not full relative paths.
- An explicitly supplied main `.tex` file bypasses `.gitignore`, while still requiring an uncommented `\documentclass` declaration.

### `compile`

```text
latexmk -file-line-error -halt-on-error -interaction=nonstopmode \
  -synctex=1 -pdfxe -auxdir=.aux -outdir=. ./main.tex
```

The command runs with the document directory as its working directory.

### `clean`

```text
latexmk -c -auxdir=.aux -outdir=. ./main.tex
```

This delegates cleanup to latexmk and preserves final PDF/DVI/PostScript output.
Project-specific `.latexmkrc` cleanup rules remain effective, and an empty `.aux/` directory may remain afterward.

### `clean-compile`

This task contains an ordered clean step followed by compile.
A failed clean skips compilation for that document.
Different document tasks remain eligible for parallel execution, including documents in the same directory.

This deliberately uses `latexmk -c` followed by compilation instead of `latexmk -gg`.
The latter performs `-C`-style cleanup and may delete the existing PDF before recompilation, whereas `clean-compile` preserves the previous final document if the new compilation fails.

Before local workers start, required `.aux/` directories are created once to avoid first-run directory-creation races.
Exported compile tasks include the equivalent idempotent platform command.

## ▶️ Actions

### `run`

Runs tasks with up to `--jobs` workers.
The default is `min(4, CPU count)`.
Each latexmk subprocess has its own `--timeout`, which defaults to 180 seconds.
For `clean-compile`, clean and compile each receive the full timeout.
A failed document does not prevent other independent document tasks from finishing.

`--output-mode` is available only for this action:

- `progressbar` is the default interactive display.
- `list` writes stable completion lines to stdout.
- `json` writes one schema-v2 document to stdout.

The three modes describe completed execution, not the task plan.
Consequently, they do not apply to `preview` or `export`.

### `preview`

Builds the complete plan, including engines and exact `cwd + argv`, but executes nothing and writes no engine-cache updates.
Preview always uses a human-readable list on stdout.

### `export`

Writes an executable script to stdout and no status text there.
The syntax is selected from the current platform by default:

- Windows: PowerShell 7.
- Linux/macOS/POSIX: `sh`.

Use `--export-format` to override the automatic selection:

```bash
# Auto-detect (default) — PowerShell on Windows, sh elsewhere
auto-latexmk --action export > compile.sh

# Force PowerShell regardless of platform
auto-latexmk --action export --export-format pwsh > compile.ps1

# Force bash/sh regardless of platform
auto-latexmk --action export --export-format bash > compile.sh
```

Paths are relative to the scanned root.
Each document task occupies one physical line; clean-compile remains one compound task.
The complete script continues after independent failures and exits nonzero if any task failed.
If a task directory cannot be entered, that task fails without running its commands from the caller's directory.

Only the generated script and TeX Live are needed on the target machine; `auto-latexmk` itself is not required there.
By default, export selects PowerShell on Windows and POSIX `sh` elsewhere; use `--export-format` to override the automatic selection.
Run the exported script from the directory that was scanned; for a single-file input, this is the source file's directory.
Local `--jobs` and `--timeout` settings are intentionally not encoded into the script, avoiding extra scheduler or platform timeout dependencies.

An empty task plan is successful rather than exceptional.
Run reports zero completed tasks, preview prints `0 task(s) planned`, and export emits a valid no-op script that exits successfully.

## 🖥️ Output examples

Paths, timings, and progress-bar animation vary between runs.
The examples below show the stable shape of each output channel.

| Content | Channel |
| --- | --- |
| Interactive progress bar | stderr |
| Run list and task failure details | stdout |
| Preview plan | stdout |
| JSON result | stdout |
| Exported script | stdout |
| Warnings, setup errors, and ordinary diagnostics | stderr |
| Debug log | The file passed to `--debug-log` |

JSON is the one exception to ordinary error routing: setup failures are returned as a structured JSON document on stdout so consumers never receive a mixture of JSON and prose.

### Run: progress bar

The default `--action run --output-mode progressbar` writes an updating bar to stderr:

```console
$ auto-latexmk
Compile  █████████▍        17/29  00:03
```

The compact, borderless bar shows completed tasks out of the total and elapsed time.
A failure count (for example, `1 failed`) appears only if a task fails.
In color terminals, completed cells are green; cells containing a failed task stay red.
Cells follow completion order. With more than 16 tasks, each cell represents multiple tasks.
After the bar closes, any failed tasks are printed with their extracted LaTeX error and log path.

### Run: completion list

`--output-mode list` writes one line to stdout when each task completes, followed by a batch summary:

```console
$ auto-latexmk --output-mode list
[1/3] OK     /project/notes-a.tex (xelatex) (0.61s)
[2/3] FAILED /project/notes-b.tex (pdflatex) (0.72s)
[3/3] OK     /project/report.tex (lualatex) (1.08s)
Processed 3 task(s) in 1.12s: 2 succeeded, 1 failed, 0 cancelled

FAILED  /project/notes-b.tex (compile) (0.72s)
  compile: compile failed with exit code 12
    ! Undefined control sequence.
    l.4 \\undefinedcommand
    Log: /project/.aux/notes-b.log
```

With concurrent jobs, lines appear in completion order rather than discovery order.
For a clean task, the parenthesized detail is `clean` instead of an engine name.

### Run: JSON result

JSON mode writes one document to stdout and no interactive progress.
A successful single-document run resembles:

```json
{
  "schema_version": 2,
  "task_type": "compile",
  "action": "run",
  "status": "succeeded",
  "elapsed_time": 0.814,
  "summary": {
    "total": 1,
    "succeeded": 1,
    "failed": 0,
    "cancelled": 0
  },
  "tasks": [
    {
      "index": 1,
      "path": "/project/main.tex",
      "task_type": "compile",
      "engine": "pdflatex",
      "engine_reason": "command-line option",
      "status": "succeeded",
      "elapsed_time": 0.801,
      "steps": [
        {
          "step": "compile",
          "status": "succeeded",
          "cwd": "/project",
          "argv": ["latexmk", "-file-line-error", "-halt-on-error", "-interaction=nonstopmode", "-synctex=1", "-pdf", "-auxdir=.aux", "-outdir=.", "./main.tex"],
          "elapsed_time": 0.8,
          "exit_code": 0,
          "timed_out": false
        }
      ]
    }
  ]
}
```

See the JSON section below for the corresponding failure shape and consumer guidance.

### Preview: ordered task plan

Preview writes the resolved plan to stdout.
This example makes the two steps of `clean-compile` visible without executing either command:

```console
$ auto-latexmk --task clean-compile --action preview --engine pdflatex main.tex
1 task(s) planned:
  [1/1] /project/main.tex (clean-compile; pdflatex)
    clean: cwd=/project latexmk -c -auxdir=.aux -outdir=. ./main.tex
    compile: cwd=/project latexmk -file-line-error -halt-on-error -interaction=nonstopmode -synctex=1 -pdf -auxdir=.aux -outdir=. ./main.tex
```

### Export: executable script

Export reserves stdout for the script, so normal redirection produces a file without status messages mixed into it.
On POSIX, a one-document compile export has this shape:

```console
$ auto-latexmk --action export --engine pdflatex main.tex
#!/bin/sh
auto_latexmk_status=0
(cd . && mkdir -p .aux && latexmk -file-line-error -halt-on-error -interaction=nonstopmode -synctex=1 -pdf -auxdir=.aux -outdir=. ./main.tex); auto_latexmk_rc=$?; [ "$auto_latexmk_rc" -eq 0 ] || auto_latexmk_status=1; (exit "$auto_latexmk_rc")
exit "$auto_latexmk_status"
```

The equivalent Windows output is PowerShell.
Each task is still emitted on one physical line; the middle line is shortened here only for readability:

```powershell
$script:autoLatexmkFailed = $false
$script:autoLatexmkTaskFailed = $false; & { try { Push-Location '.' -ErrorAction Stop; try { $ErrorActionPreference = 'Stop'; New-Item ...; & 'latexmk' ... } finally { Pop-Location } } catch { $script:autoLatexmkTaskFailed = $true; Write-Error $_ -ErrorAction Continue } }; if ($script:autoLatexmkTaskFailed) { $script:autoLatexmkFailed = $true }
if ($script:autoLatexmkFailed) { exit 1 }
```

Independent task failures are accumulated, and the final script exits with code 1 if any task failed.

## 🧠 Engine selection and cache

With `--engine auto`, each document uses:

1. A valid `% !TEX` directive on its first line.
2. The last successful engine in the preference cache.
3. XeLaTeX as the fallback.

The cache remains compatible with earlier releases:

```text
~/.cache/auto-latexmk/compiler-preferences.json
```

Only successful `compile` or `clean-compile` runs update it.
Preview/export may read it but never write it.
Use `--no-engine-cache` for reproducible planning and export.

| Task and action | Read cache | Write cache |
| --- | ---: | ---: |
| Compile/clean-compile + run | Yes | After successful compilation |
| Compile/clean-compile + preview/export | Yes | No |
| Clean with any action | No | No |

Cache keys are canonical absolute document paths and use `/` separators on Windows as well as POSIX.
The cache is merged and written atomically with process-local and cross-process locking; invalid engine entries are ignored when read.

## 🤖 JSON output

```bash
auto-latexmk --output-mode json > result.json
```

Schema version 2 reports each task and each clean/compile step separately.
It includes `cwd`, the unjoined `argv` array, elapsed time, exit code, timeout state, engine selection reason, log path, and a bounded error excerpt suitable for programmatic or LLM-assisted diagnosis.
Full compiler output remains available through `--debug-log`.

The top-level shape is stable and intentionally task-oriented:

```json
{
  "schema_version": 2,
  "task_type": "compile",
  "action": "run",
  "status": "failed",
  "elapsed_time": 0.827,
  "summary": {
    "total": 1,
    "succeeded": 0,
    "failed": 1,
    "cancelled": 0
  },
  "tasks": [
    {
      "index": 1,
      "path": "/work/notes/main.tex",
      "task_type": "compile",
      "engine": "pdflatex",
      "engine_reason": "command-line option",
      "status": "failed",
      "elapsed_time": 0.812,
      "steps": [
        {
          "step": "compile",
          "status": "failed",
          "cwd": "/work/notes",
          "argv": ["latexmk", "-file-line-error", "-halt-on-error", "-interaction=nonstopmode", "-synctex=1", "-pdf", "-auxdir=.aux", "-outdir=.", "./main.tex"],
          "elapsed_time": 0.801,
          "exit_code": 12,
          "timed_out": false,
          "error": {
            "kind": "latex-log",
            "message": "compile failed with exit code 12",
            "excerpt": "! Undefined control sequence.\nl.8 \\\\undefinedcommand",
            "log_file": "/work/notes/.aux/main.log"
          }
        }
      ]
    }
  ]
}
```

The success and failure examples use the same schema.
Successful steps omit `error`; failed steps include it when diagnostic information is available.
Consumers should branch on `schema_version` and use the `argv` array rather than parsing a shell string.

The remaining schema guarantees are:

- Top-level status is `succeeded`, `failed`, or `interrupted`.
- Task status is `succeeded`, `failed`, or `cancelled`.
- Step status is `succeeded`, `failed`, `cancelled`, or `skipped`.
- A clean task has `null` engine fields, and a failed clean causes the following compile step to be `skipped`.
- `error.kind` is one of `latex-log`, `process-output`, `timeout`, `cancelled`, `exception`, or `setup`.
- Error excerpts are bounded; complete compiler output belongs in `--debug-log`.
- Tasks are sorted by discovery index even when concurrent execution completes in a different order.

## 🚦 Exit status

| Code | Meaning |
| ---: | --- |
| `0` | All tasks succeeded, or preview/export completed successfully |
| `1` | Discovery, planning, cleaning, compilation, export, or setup failed |
| `2` | Invalid command-line usage or option combination |
| `130` | Interrupted with Ctrl+C |

## 🏗️ Architecture

The code follows the same plan-first model as the CLI.
Discovery and engine selection produce immutable task data; actions then consume it without having to rediscover or reinterpret documents.

```text
cli → planning → TaskPlan → execution → reporting
                        ↘ preview/export
       cache/runtime support the pipeline
```

| Module | Responsibility |
| --- | --- |
| `cli.py` | Parse and validate option scopes; coordinate one invocation |
| `planning.py` | Discover main files, select engines, and build exact commands |
| `models.py` | Define plans, tasks, command steps, results, and JSON conversion |
| `execution.py` | Run task steps, enforce timeouts, collect logs, and coordinate workers |
| `actions.py` | Render a plan as preview text or a platform-native script |
| `reporting.py` | Present run results as progress, stable lines, or schema-v2 JSON |
| `cache.py` | Read and atomically update successful engine preferences |
| `runtime.py` | Shared logging, exceptions, and process utilities |

`auto_latexmk.py` remains a thin compatibility entry point; new behavior belongs in the focused modules above.
Tasks are independent scheduling units.
Steps inside `clean-compile` are ordered, while separate tasks may run concurrently—even when their source files share a directory, as those projects are assumed to be independent.

`TaskPlan` is the boundary between construction and consumption.
Execution, preview, and export may render or run its existing `cwd + argv` command specifications, but must not independently select engines, reinterpret cleaning, or reconstruct latexmk arguments.
This invariant keeps the three actions consistent.

## 🧪 Testing and development

Install the project environment, then run the complete suite:

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

The suite has two layers:

- Fast tests under `tests/test_auto_latexmk/` exercise parsing, validation, planning, caching, export rendering, execution control, and reporting.
- Integration tests under `tests/test_integration/` copy checked-in `.tex` project trees to isolated temporary directories and invoke the real CLI and TeX Live.
  The fixtures contain multiple main documents, nested `\input` fragments, shared files, cross-references, and an ignored broken draft.
  The tests verify recursive discovery, compile/clean/clean-compile, exported script execution, and machine-readable diagnostics from a deliberate LaTeX failure.
  The English test creates its `.gitignore` only in the temporary copy, keeping the intentionally ignored draft visible to Git in the fixture source tree.
  A separate Chinese project verifies `% !TEX` engine selection and real `ctexart` compilation.
  Individual groups are skipped when their required `latexmk`, engine, or TeX class is unavailable.

Run only the real TeX Live tests with:

```bash
uv run python -m unittest discover -s tests/test_integration -v
```

Static, lockfile, and package checks:

```bash
uvx ruff check src tests
uvx pyright --pythonpath .venv/Scripts/python.exe src tests
uv lock --check
uv build
```
