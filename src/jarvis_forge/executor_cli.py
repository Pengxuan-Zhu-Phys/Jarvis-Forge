"""jforge bench executor: store site execution settings outside bench cards."""

from pathlib import Path

import typer

from . import bench_exec


app = typer.Typer(help="Store local and Slurm executor settings.",
                  context_settings={"help_option_names": ["-h", "--help"]})


def _echo(ctx, command, result):
    from .cli import emit
    if ctx.obj.as_json:
        emit(ctx, command, **{key: value for key, value in result.items() if key != "text"})
    else:
        typer.echo(result.get("text") or "", nl=False)


@app.command("add")
def add(ctx: typer.Context,
        name: str = typer.Argument(..., help="Executor name.", metavar="<name>"),
        source: Path = typer.Option(..., "--file", help="Executor YAML to copy in.", metavar="<site.yaml>")):
    """Validate an executor file and store it under the Forge home."""
    _echo(ctx, "executor add", bench_exec.add_executor(ctx.obj.home, name, source))


@app.command("list")
def list_executors(ctx: typer.Context):
    """List stored executors."""
    _echo(ctx, "executor list", bench_exec.list_executors(ctx.obj.home))


@app.command("show")
def show(ctx: typer.Context, name: str = typer.Argument(..., help="Executor name.", metavar="<name>")):
    """Show one stored executor."""
    _echo(ctx, "executor show", bench_exec.show_executor(ctx.obj.home, name))
