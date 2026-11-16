"""Regression checks with full and declared-input selection modes."""

from pathlib import Path
from typing import Optional
import typer

from . import checker


def _schema2(config):
    """jforge checker hands schema 2 cards to the jforge test engine."""
    from . import testing
    _, _, data = testing._read(config)
    return data.get("kind") == "checker" and data.get("schema_version") == 2

app = typer.Typer(help="Run full or affected regression checks with archived evidence.",
                  context_settings={"help_option_names": ["-h", "--help"]})


@app.command("list")
def list_checks(ctx: typer.Context, config: Path = typer.Argument(..., metavar="<checker_yaml>")):
    """List checks, input patterns and prerequisites without executing them."""
    from .cli import emit
    if _schema2(config):
        from . import testing
        emit(ctx, "test plan", **testing.plan(config))
        return
    loaded = checker.load(config)
    emit(ctx, "checker list", checker_id=loaded["config"]["id"], project=loaded["project"], checks=loaded["ordered"])


@app.command()
def validate(ctx: typer.Context, config: Path = typer.Argument(..., metavar="<checker_yaml>")):
    """Validate checker inputs and dependency ordering."""
    from .cli import emit
    if _schema2(config):
        from . import testing
        emit(ctx, "test validate", **testing.validate(config))
        return
    loaded = checker.load(config)
    emit(ctx, "checker validate", checker_id=loaded["config"]["id"], checks=len(loaded["ordered"]))


@app.command()
def run(ctx: typer.Context, config: Path = typer.Argument(..., metavar="<checker_yaml>"),
        full: bool = typer.Option(False, "--full", help="Execute every selected check, bypassing successful check reuse."),
        check: list[str] = typer.Option([], "--check", help="Select check IDs and their prerequisite closure."),
        env: Optional[str] = typer.Option(None, "--env", help="Use this registered compiler/Python environment."),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Concurrent build jobs inside build checks.")):
    """Run affected checks by default; the first run executes all checks."""
    from .cli import emit, _console, line
    def progress(text):
        line(_console(stderr=True), text)
    if _schema2(config):
        from . import testing_run
        result = testing_run.run(ctx.obj.home, config, groups=check, full=full, environment_name=env,
                                 jobs=jobs, progress=progress)
        emit(ctx, "test run", ok=result["status"] == "passed", **result)
        raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)
    result = checker.run(ctx.obj.home, config, full=full, selected=check, environment_name=env, jobs=jobs,
                         progress=progress)
    emit(ctx, "checker run", ok=result["status"] == "passed", **result)
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)
