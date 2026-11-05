"""APT terminal progress; raw subprocess bytes are always archived separately."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import re
import sys

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from .presentation import line


class AptProgress:
    def __init__(self, console, *, verbose=False, as_json=False, check=False):
        self.console = console
        self.verbose = verbose
        self.as_json = as_json
        self.total = 2 if check else 4
        self.stage = 0
        self.preparing = False
        self.closed = False
        self.pending = set()
        self.done = set()
        self.decoders = {}
        self.buffers = {}
        self.live = None
        self.task = None
        self.packages_task = None
        if (console.is_terminal and not console.is_dumb_terminal
                and not as_json and not verbose and not os.environ.get("NO_COLOR")):
            self.live = Progress(
                SpinnerColumn(), TextColumn("{task.description}"),
                BarColumn(bar_width=None), TextColumn("{task.completed:.0f}/{task.total:.0f}"),
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
                                 description=Text(stage[3]))
            else:
                line(self.console, f"[{self.stage}/{self.total}] {stage[3]}")
        elif message.startswith("Spack "):
            if self.verbose:
                line(self.console, message,
                     style=None if self.as_json or os.environ.get("NO_COLOR") else "dim")
            elif not self.stage and not self.preparing:
                line(self.console, "Preparing Spack environment...")
                self.preparing = True
        else:
            line(self.console, message)

    def installation_plan(self, packages, overwrite=()):
        self.pending = {p["hash"] for p in packages
                        if not p["external"] and (not p["installed"] or p["name"] in overwrite)}
        self.done = set()
        if self.live and self.pending:
            if self.packages_task is None:
                self.packages_task = self.live.add_task("Packages", total=len(self.pending))
            else:
                self.live.update(self.packages_task, completed=0, total=len(self.pending),
                                 description="Packages")

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

    def _package_status(self, message):
        # Spack's status lines are a display hint only, never installation evidence.
        message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
        status = re.match(r"\[([ +])\]\s+([a-z0-9]+)\s+(\S+@\S+)", message)
        if not status or self.packages_task is None:
            return
        matches = {h for h in self.pending if h.startswith(status[2])}
        if not matches:
            return
        if status[1] == "+":
            self.done.update(matches)
        self.live.update(self.packages_task, completed=len(self.done),
                         description=Text("Installing " + status[3]))

    def command_failure(self, number, code, stderr):
        title = {"concretize": "Dependency resolution", "install": "Package installation",
                 "install-cache": "Binary installation"}.get(str(number), "Spack " + str(number))
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
                self.live.update(self.task, completed=self.stage)
                if self.packages_task is not None:
                    self.live.update(self.packages_task, completed=len(self.pending))
            self.live.stop()
