import sys

from tqdm import tqdm

from auto_latexmk.models import RunSummary, TaskResult, TaskType


class _TaskProgressBar(tqdm):
    BAR_WIDTH = 16

    def __init__(self, *args, **kwargs):
        self._failed_cells = set()
        super().__init__(*args, **kwargs)

    def record_result(self, success: bool):
        if not success and self.total:
            start = self.n * self.BAR_WIDTH // self.total
            end = ((self.n + 1) * self.BAR_WIDTH + self.total - 1) // self.total
            self._failed_cells.update(range(start, min(end, self.BAR_WIDTH)))
        self.update(1)

    @property
    def format_dict(self):
        data = super().format_dict
        bar_data = dict(data, bar_format=f"{{bar:{self.BAR_WIDTH}}}", colour=None)
        cells = list(self.format_meter(**bar_data))
        if self.colour:
            for index, cell in enumerate(cells):
                failed = index in self._failed_cells
                if failed and cell == " ":
                    cell = "#" if self.ascii else "▏"
                if cell != " ":
                    code = "31" if failed else "32"
                    cells[index] = f"\033[{code}m{cell}\033[0m"
        data["task_bar"] = "".join(cells)
        return data


class Reporter:
    def __init__(self, *, task_type: TaskType, color=True, verbose=False, stream=None):
        self.task_type = task_type
        self.stream = stream if stream is not None else sys.stderr
        self.color = color and self.stream.isatty()
        self.verbose = verbose
        self._bar = None
        self._total = 0
        self._completed = 0
        self._failed = 0

    def _paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def start(self, tasks):
        self._total = len(tasks)
        if not tasks:
            print("No LaTeX main documents found.", file=self.stream)
        elif self.stream.isatty() and not self.verbose:
            self._bar = _TaskProgressBar(
                total=len(tasks),
                desc=self.task_type.value.capitalize(),
                bar_format=(
                    "{desc}  {task_bar}  "
                    f"{{n_fmt:>{len(str(self._total))}}}/{{total_fmt}}  "
                    "{elapsed}{postfix}"
                ),
                colour="green" if self.color else None,
                file=self.stream,
            )

    def task_finished(self, result: TaskResult):
        self._completed += 1
        if self._bar is not None:
            self._failed += not result.success
            self._bar.set_postfix_str(
                f"{self._failed} failed/cancelled" if self._failed else "",
                refresh=False,
            )
            self._bar.record_result(result.success)
        else:
            status = (
                "OK"
                if result.success
                else "CANCELLED"
                if result.cancelled
                else "FAILED"
            )
            label = self._paint(
                status, "32" if result.success else "33" if result.cancelled else "31"
            )
            print(
                f"[{self._completed}/{self._total}] {label}  {result.task.tex_file} "
                f"({result.task.engine or result.task.task_type.value}) ({result.elapsed_time:.2f}s)",
                file=self.stream,
            )
        if self.verbose and result.output_file:
            print(f"\nFull output: {result.output_file}", file=self.stream)
            try:
                with open(
                    result.output_file, encoding="utf-8", errors="replace"
                ) as output:
                    for line in output:
                        self.stream.write(line)
            except OSError as error:
                print(f"Could not read full output: {error}", file=self.stream)
        self.stream.flush()

    def finish(self, summary: RunSummary):
        self.close()
        print(
            f"Processed {summary.total} document(s) in {summary.elapsed_time:.2f}s: "
            f"{summary.succeeded} succeeded, {summary.failed} failed, {summary.cancelled} cancelled",
            file=self.stream,
        )
        for result in sorted(summary.results, key=lambda item: item.task.index):
            if result.log_error:
                print(f"WARNING: {result.log_error}", file=self.stream)
            if result.success:
                continue
            label = "CANCELLED" if result.cancelled else "FAILED"
            print(
                f"\n{self._paint(label, '33' if result.cancelled else '31')}  {result.task.tex_file}",
                file=self.stream,
            )
            if result.task.engine:
                print(f"  Engine: {result.task.engine}", file=self.stream)
            for step in result.steps:
                if step.status not in {"failed", "cancelled"}:
                    continue
                print(
                    f"  {step.command.step.value}: {step.error_message}",
                    file=self.stream,
                )
                if step.error_excerpt:
                    for line in step.error_excerpt.splitlines():
                        print(f"    {line}", file=self.stream)
                if step.log_file:
                    print(f"  TeX log: {step.log_file}", file=self.stream)
            if result.output_file:
                print(f"  Full output: {result.output_file}", file=self.stream)

    def fatal_error(self, message):
        self.close()
        print(f"ERROR: {message}", file=self.stream)

    def close(self):
        if self._bar is not None:
            self._bar.close()
            self._bar = None
