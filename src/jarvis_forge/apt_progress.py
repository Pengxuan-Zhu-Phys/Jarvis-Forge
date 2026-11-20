"""APT terminal progress; raw subprocess bytes are always archived separately."""

from __future__ import annotations

import codecs
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys

from rich.progress import BarColumn, DownloadColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .presentation import line


class _KnownTotalColumn(MofNCompleteColumn):
    def __init__(self):
        super().__init__()
        self.download = DownloadColumn(binary_units=True)

    def render(self, task):
        if task.fields.get("download"):
            return self.download.render(task)
        if task.fields.get("kind") == "stage":
            return Text()
        if task.fields.get("kind") == "packages":
            return Text(f"{int(task.completed)}/{int(task.total)} · {task.percentage:.0f}%")
        return Text() if task.total is None else super().render(task)


def _duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class _TotalElapsedColumn(TimeElapsedColumn):
    def render(self, task):
        return Text("total " + _duration(task.elapsed))


class _InstallProgress(Progress):
    """Separate the aggregate bars from the aligned, responsive package rows."""

    def get_renderables(self):
        tasks = [task for task in self.tasks if task.visible]
        yield self.make_tasks_table([task for task in tasks if task.fields.get("kind") != "package"])
        active = [task for task in tasks if task.fields.get("kind") == "package"]
        if not active:
            return
        compact = self.console.width < 72
        activity = self.console.width >= 80
        table = Table(box=None, padding=(0, 1), expand=True, header_style="dim")
        table.add_column("", width=1)
        table.add_column("Package / phase" if compact else "Package", ratio=2)
        if not compact:
            table.add_column("Phase", ratio=1)
        if activity:
            table.add_column("Activity", width=8)
        if compact:
            table.add_column("Time: phase / total", justify="right")
        else:
            table.add_column("Phase time", justify="right")
            table.add_column("Total time", justify="right")
        for task in active:
            phase = task.fields["phase"]
            name = task.fields["package"]
            row = [self.columns[0].render(task), Text(name + ("\n" + phase if compact else ""))]
            if not compact:
                row.append(Text(phase, style="cyan"))
            if activity:
                row.append(ProgressBar(total=None, width=8, pulse=True, animation_time=task.get_time()))
            phase_time = _duration(task.get_time() - task.fields["phase_started"])
            total_time = _duration(task.elapsed)
            row.extend([Text(phase_time + " / " + total_time)] if compact else [Text(phase_time), Text(total_time)])
            table.add_row(*row)
        yield table


class AptProgress:
    def __init__(self, console, *, verbose=False, as_json=False, check=False, indent=""):
        self.console = console
        self.verbose = verbose
        self.as_json = as_json
        self.indent = indent
        self.total = 2 if check else 4
        self.stage = 0
        self.preparing = False
        self.closed = False
        self.pending = set()
        self.done = set()
        self.failed = set()
        self.in_flight = {}
        self.package_labels = {}
        self.completed_rows = []
        self.reported = set()
        self.decoders = {}
        self.buffers = {}
        self.live = None
        self.task = None
        self.packages_task = None
        if (console.is_terminal and not console.is_dumb_terminal
                and not as_json and not verbose and not os.environ.get("NO_COLOR")):
            self.live = _InstallProgress(
                SpinnerColumn(), TextColumn("{task.description}", markup=False),
                BarColumn(bar_width=None), _KnownTotalColumn(),
                _TotalElapsedColumn(), console=console, transient=True,
                redirect_stdout=False, redirect_stderr=False,
            )

    def __call__(self, message):
        stage = re.match(r"\[(\d+)/(\d+)\]\s+(.+)", message)
        if stage:
            self.stage = int(stage[1])
            if self.live:
                if self.task is None:
                    self.task = self.live.add_task("", total=self.total, kind="stage")
                    self.live.start()
                self.live.update(self.task, completed=self.stage - 1,
                                 description=f"Step {self.stage}/{self.total} · {stage[3]}")
            else:
                line(self.console, f"{self.indent}[{self.stage}/{self.total}] {stage[3]}")
        elif message.startswith("Spack "):
            if self.verbose:
                line(self.console, message,
                     style=None if self.as_json or os.environ.get("NO_COLOR") else "dim")
            elif not self.stage and not self.preparing:
                line(self.console, self.indent + "Preparing Spack environment...")
                self.preparing = True
        else:
            line(self.console, self.indent + message)

    @contextmanager
    def activity(self, description, *, download=False):
        if not self.live:
            self(description)
            yield None
            return

        started_here = not self.live.live.is_started
        task = self.live.add_task(description, total=None, download=download)
        if started_here:
            self.live.start()

        def update(completed, total):
            self.live.update(task, completed=completed, total=total, refresh=True)

        try:
            yield update if download else None
        finally:
            self.live.remove_task(task)
            if started_here:
                self.live.stop()

    def installation_plan(self, packages, overwrite=()):
        self.package_labels.update({p["hash"]: p["name"] + ("@" + str(p["version"]) if p.get("version") else "")
                                    for p in packages})
        self.pending = {p["hash"] for p in packages
                        if not p["external"] and (not p["installed"] or p["name"] in overwrite)}
        self.done.clear()
        self.failed.clear()
        if self.live and self.pending and self.packages_task is None:
            self.packages_task = self.live.add_task("Packages", total=len(self.pending), kind="packages")
        self._clear_in_flight()

    def _update_packages(self):
        if self.packages_task is not None:
            queued = max(0, len(self.pending) - len(self.done) - len(self.in_flight) - len(self.failed))
            description = f"Packages · {len(self.in_flight)} running · {queued} queued"
            if self.failed:
                description += f" · {len(self.failed)} failed"
            self.live.update(self.packages_task, completed=len(self.done), total=len(self.pending),
                             description=description, visible=bool(self.pending))

    def _clear_in_flight(self):
        for task in self.in_flight.values():
            self.live.remove_task(task)
        self.in_flight.clear()
        self._update_packages()

    def _retain_package(self, digest, package, status, task_id=None):
        """Print one permanent receipt above the live area, never recycle it."""
        if (digest, status) in self.reported:
            return
        self.reported.add((digest, status))
        task = next((row for row in self.live.tasks if row.id == task_id), None)
        elapsed = task.elapsed if task else None
        phase = task.fields["phase"] if task else None
        phase_time = task.get_time() - task.fields["phase_started"] if task else None
        self.completed_rows.append({"hash": digest, "package": package, "status": status,
                                    "phase": phase, "phase_seconds": phase_time, "elapsed_seconds": elapsed})
        receipt = Text(self.indent + ("✓ " if status == "installed" else "✗ "),
                       style="green" if status == "installed" else "red")
        receipt.append(package, style="default")
        receipt.append(" · " + status)
        if phase is not None:
            receipt.append(f" · {phase} {_duration(phase_time)} · total {_duration(elapsed)}", style="dim")
        # Console.print integrates with Rich Live: receipts enter terminal
        # scrollback, while only the running rows below keep refreshing.
        self.console.print(receipt, soft_wrap=True)

    def output(self, stream, data):
        if self.verbose:
            # Match the archived bytes, including partial lines and whitespace.
            if hasattr(sys.stderr, "buffer"):
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
            else:
                sys.stderr.write(self._decoder(stream).decode(data))
                sys.stderr.flush()
        elif self.live and self.stage == 3:
            self.buffers[stream] = self.buffers.get(stream, "") + self._decoder(stream).decode(data)
            parts = re.split(r"[\r\n]", self.buffers[stream])
            self.buffers[stream] = parts.pop()
            for part in parts:
                self._package_status(part)

    def _decoder(self, stream):
        if stream not in self.decoders:
            self.decoders[stream] = codecs.getincrementaldecoder("utf-8")("replace")
        return self.decoders[stream]

    def output_finished(self):
        for stream, decoder in self.decoders.items():
            remaining = decoder.decode(b"", final=True)
            if self.verbose and not hasattr(sys.stderr, "buffer"):
                sys.stderr.write(remaining)
            elif not self.verbose:
                self._package_status(self.buffers.get(stream, "") + remaining)
        self.decoders.clear()
        self.buffers.clear()
        self._clear_in_flight()

    def _package_status(self, message):
        # Spack's status lines are a display hint only, never installation evidence.
        message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
        if self.packages_task is None:
            return
        # Concurrent configure/build output can leave an unfinished line before
        # a status, or join several statuses on one line. Match event headers
        # wherever they occur, while still requiring an unambiguous planned hash.
        events = list(re.finditer(r"\[([ +x])\]\s+([a-z0-9]+)\s+(\S+@\S+)", message))
        for index, event in enumerate(events):
            end = events[index + 1].start() if index + 1 < len(events) else len(message)
            self._package_event(*event.groups(), message[event.end():end].strip())

    def _package_event(self, marker, short_hash, package, detail):
        matches = [h for h in self.pending if h.startswith(short_hash)]
        if len(matches) != 1:
            return
        digest = matches[0]
        if marker == " ":
            phase = re.split(r"\s+\((?:\d+(?:\.\d+)?[hms])+\)", detail or "", maxsplit=1)[0].strip()
            if not phase or digest in self.done:
                return
            if phase != "no binary available" and not phase.startswith("stopped "):
                self.failed.discard(digest)
                description = f"  {package} · {phase}"
                if digest in self.in_flight:
                    task = next(task for task in self.live.tasks if task.id == self.in_flight[digest])
                    started = (task.fields["phase_started"] if task.fields["phase"] == phase
                               else self.live.get_time())
                    self.live.update(task.id, description=description, phase=phase, phase_started=started)
                else:
                    # Spack's piped status reports phases without a completion fraction.
                    self.in_flight[digest] = self.live.add_task(
                        description, total=None, kind="package", package=package, phase=phase,
                        phase_started=self.live.get_time())
                self._update_packages()
                return
        if marker == "+":
            self.done.add(digest)
            self.failed.discard(digest)
        elif marker == "x":
            self.failed.add(digest)
        task = self.in_flight.pop(digest, None)
        if marker in {"+", "x"}:
            self._retain_package(digest, package, "installed" if marker == "+" else "failed", task)
        if task is not None:
            self.live.remove_task(task)
        self._update_packages()

    def command_failure(self, number, code, stderr):
        title = {
            "concretize": "Dependency resolution",
            "concretize-cache-retry": "Source dependency resolution",
            "install": "Package installation",
            "install-cache": "Binary installation",
            "install-binaries": "Remaining binary installation",
            "install-source-retry": "Source installation",
            "bootstrap-patch-configure": "Configure GNU patch",
            "bootstrap-patch-build": "Build GNU patch",
            "bootstrap-patch-verify": "Verify GNU patch",
        }.get(str(number), "Spack " + str(number))
        message = f"{title} failed (exit {code})."
        if not self.verbose:
            # Keep the complete original line; full stdout/stderr stay in files.
            error = None
            block = []
            collecting = False
            with Path(stderr).open(encoding="utf-8", errors="replace") as source:
                for text in source:
                    if "Error:" in text or "error:" in text:
                        error = text.strip().removeprefix("==> ")
                    if text.lstrip().startswith(("==> Error:", "Error:")):
                        block = [text.rstrip().removeprefix("==> ")]
                        collecting = True
                    elif collecting and text[:1].isspace() and text.strip():
                        block.append(text.rstrip())
                    else:
                        collecting = False
            if block or error:
                message += "\n" + ("\n".join(block) if block else error)
        return message

    def close(self, *, success=False):
        if self.closed:
            return
        self.closed = True
        if self.live and self.task is not None:
            if success:
                # The caller has validated the installed snapshot; missing display
                # events must not leave a successful operation showing queued work.
                for digest in sorted(self.pending - self.done):
                    self._retain_package(digest, self.package_labels.get(digest, "package"), "installed",
                                         self.in_flight.get(digest))
                self.done.update(self.pending)
                self.failed.clear()
            self._clear_in_flight()
            if success:
                self.live.update(self.task, completed=self.stage)
                if self.packages_task is not None:
                    self.live.update(self.packages_task, completed=len(self.pending))
            self.live.stop()
