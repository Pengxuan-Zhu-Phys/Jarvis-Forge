"""Manage compiler suites independently of project package installations."""

from pathlib import Path
from typing import Optional

import typer

from . import toolchains
from .apt_progress import AptProgress
from .cli import _console, emit

app = typer.Typer(help="Install and verify user-local Linux compiler toolchains.",
                  context_settings={"help_option_names": ["-h", "--help"]})


@app.command("list")
def list_toolchains(ctx: typer.Context):
    """List installed toolchains."""
    emit(ctx, "toolchain list", toolchains=toolchains.list_installed(ctx.obj.home))


@app.command()
def install(ctx: typer.Context, spec: str = typer.Argument(..., metavar="<gcc@version>"),
            target: Optional[str] = typer.Option(None, "--target", help="Select a Spack CPU target."),
            stage: Optional[Path] = typer.Option(None, "--stage", help="Use an existing directory for temporary builds."),
            timeout: float = typer.Option(14400, "--timeout", min=1, help="Timeout per Spack command in seconds."),
            cache: str = typer.Option("never", "--cache", help="Binary cache policy: auto, only or never.")):
    """Install an exact GCC, G++ and GFortran suite in a separate profile."""
    jobs = None
    progress = AptProgress(_console(stderr=True), verbose=ctx.obj.verbose, as_json=ctx.obj.as_json)
    ctx.call_on_close(progress.close)
    result = toolchains.install(ctx.obj.home, spec, target=target, stage=stage, jobs=jobs,
                                timeout=timeout, cache=cache, progress=progress)
    progress.close(success=True)
    emit(ctx, "toolchain install", **result)


@app.command()
def use(ctx: typer.Context, name: str = typer.Argument(..., metavar="<toolchain>")):
    """Bind an installed toolchain to the selected project Spack profile."""
    progress = AptProgress(_console(stderr=True), verbose=ctx.obj.verbose, as_json=ctx.obj.as_json)
    ctx.call_on_close(progress.close)
    result = toolchains.use(ctx.obj.home, name, progress=progress)
    progress.close(success=True)
    emit(ctx, "toolchain use", **result)


@app.command()
def check(ctx: typer.Context, name: str = typer.Argument(..., metavar="<toolchain>")):
    """Compile, link and run a C, C++ and Fortran probe on this node."""
    emit(ctx, "toolchain check", **toolchains.check(ctx.obj.home, name))
