"""APT terminal progress; raw subprocess bytes are always archived separately."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import re
import sys

from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from .presentation import line


class _KnownTotalColumn(MofNCompleteColumn):
    def render(self, task):
        return Text() if task.total is None else super().render(task)


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
        self.in_flight = {}
        self.decoders = {}
        self.buffers = {}
        self.live = None
        self.task = None
        self.packages_task = None
        if (console.is_terminal and not console.is_dumb_terminal
                and not as_json and not verbose and not os.environ.get("NO_COLOR")):
            self.live = Progress(
                SpinnerColumn(), TextColumn("{task.description}", markup=False),
                BarColumn(bar_width=None), _KnownTotalColumn(),
                TimeElapsedColumn(), console=console, transient=True,
                redirect_stdout=False, redirect_stderr=False,
            )

    def __call__(self, message):
        stage = re.match(r"\[(\d+)/(\d+)\]\s+(.+)", message)
        if stage:
            self.stage = int(stage[1])
            if self.live:
                if self.task is None:
                    self.task = self.live.add_task("", total=self.total)
                    self.live.start()
                self.live.update(self.task, completed=self.stage - 1,
                                 description=stage[3])
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

    def installation_plan(self, packages, overwrite=()):
        self.pending = {p["hash"] for p in packages
                        if not p["external"] and (not p["installed"] or p["name"] in overwrite)}
        self.done.clear()
        if self.live and self.pending and self.packages_task is None:
            self.packages_task = self.live.add_task("Packages", total=len(self.pending))
        self._clear_in_flight()

    def _update_packages(self):
        if self.packages_task is not None:
            self.live.update(self.packages_task, completed=len(self.done), total=len(self.pending),
                             description=f"Packages · {len(self.in_flight)} in flight", visible=bool(self.pending))

    def _clear_in_flight(self):
        for task in self.in_flight.values():
            self.live.remove_task(task)
        self.in_flight.clear()
        self._update_packages()

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
        status = re.fullmatch(r"\[([ +x])\]\s+([a-z0-9]+)\s+(\S+@\S+)(?:\s+(.*))?", message)
        if not status or self.packages_task is None:
            return
        marker, short_hash, package, detail = status.groups()
        matches = [h for h in self.pending if h.startswith(short_hash)]
        if len(matches) != 1:
            return
        digest = matches[0]
        if marker == " ":
            phase = re.sub(r"\s+\([^()]*\)$", "", detail or "")
            if not phase or digest in self.done:
                return
            if phase != "no binary available" and not phase.startswith("stopped "):
                description = f"  {package} · {phase}"
                if digest in self.in_flight:
                    self.live.update(self.in_flight[digest], description=description)
                else:
                    # Spack's piped status reports phases without a completion fraction.
                    self.in_flight[digest] = self.live.add_task(description, total=None)
                    self._update_packages()
                return
        if marker == "+":
            self.done.add(digest)
        task = self.in_flight.pop(digest, None)
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
            self._clear_in_flight()
            if success:
                self.live.update(self.task, completed=self.stage)
                if self.packages_task is not None:
                    self.live.update(self.packages_task, completed=len(self.pending))
            self.live.stop()
