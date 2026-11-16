"""jforge test: validate, plan, run and summarize YAML test cards and catalogs over pytest.

Design: Jarvis-Books Docs/Jarvis-Forge/TEST_CATALOG_DESIGN.md.
"""

from pathlib import Path
from typing import Optional

import typer

from . import testing

app = typer.Typer(help="Validate, plan, run and accept YAML test cards and catalogs over pytest.",
                  context_settings={"help_option_names": ["-h", "--help"]})


@app.command()
def validate(ctx: typer.Context,
             config: Path = typer.Argument(..., help="Test card or test catalog YAML.", metavar="<card_or_catalog>"),
             root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>")):
    """Check cards, profiles, coverage and gate rules without running tests."""
    from .cli import emit
    emit(ctx, "test validate", **testing.validate(config, root=root))


@app.command()
def plan(ctx: typer.Context,
         config: Path = typer.Argument(..., help="Test card or test catalog YAML.", metavar="<card_or_catalog>"),
         profile: Optional[str] = typer.Option(None, "--profile", help="Select a named profile.", metavar="<name>"),
         group: list[str] = typer.Option([], "--group", help="Select group or check IDs and their prerequisites.", metavar="<id>"),
         batch: Optional[str] = typer.Option(None, "--batch", help="Show shard k of n for each pytest group.", metavar="<k/n>"),
         project: Optional[str] = typer.Option(None, "--project", help="Select one catalog card.", metavar="<id>"),
         root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>")):
    """Show selected steps, files and shards without running pytest or creating user state."""
    from .cli import emit
    emit(ctx, "test plan", **testing.plan(config, profile=profile, groups=group, batch=batch, root=root, project=project))


@app.command()
def run(ctx: typer.Context,
        config: Path = typer.Argument(..., help="Test card or test catalog YAML.", metavar="<card_or_catalog>"),
        profile: Optional[str] = typer.Option(None, "--profile", help="Select a named profile.", metavar="<name>"),
        group: list[str] = typer.Option([], "--group", help="Select group or check IDs and their prerequisites.", metavar="<id>"),
        batch: Optional[str] = typer.Option(None, "--batch", help="Run shard k of n for each pytest group.", metavar="<k/n>"),
        acceptance_id: Optional[str] = typer.Option(None, "--acceptance-id", help="Group this run with other shards or cards.", metavar="<id>"),
        full: bool = typer.Option(False, "--full", help="Run every selected step; do not reuse earlier results."),
        rerun_failed: Optional[str] = typer.Option(None, "--rerun-failed", help="Run again only what did not pass in this run, with its selection and shard; never accepted.", metavar="<run_id>"),
        env: Optional[str] = typer.Option(None, "--env", help="Use this registered environment.", metavar="<name>"),
        project: Optional[str] = typer.Option(None, "--project", help="Select one catalog card.", metavar="<id>"),
        root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>"),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Concurrent build jobs inside build checks.", metavar="<n>")):
    """Run the selected groups and checks, reuse unchanged results, and record the evidence."""
    from .cli import emit, _console, line
    from . import testing_run
    result = testing_run.run(ctx.obj.home, config, profile=profile, groups=group, batch=batch,
                             acceptance_id=acceptance_id, full=full, environment_name=env, root=root,
                             project=project, jobs=jobs, rerun_failed=rerun_failed,
                             progress=lambda text: line(_console(stderr=True), text))
    emit(ctx, "test run", ok=result["status"] == "passed", **result)
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)


@app.command()
def summarize(ctx: typer.Context,
              acceptance_id: str = typer.Option(..., "--acceptance-id", help="Acceptance to summarize.", metavar="<id>"),
              rebuild: bool = typer.Option(False, "--rebuild", help="Also rewrite the run index from the run records.")):
    """Combine the runs of one acceptance into a report; run no tests."""
    from .cli import emit
    from . import testing_run
    report = testing_run.summarize(ctx.obj.home, acceptance_id, rebuild=rebuild)
    emit(ctx, "test summarize", ok=report["status"] == "passed", **report)
    raise typer.Exit(0 if report["status"] == "passed" else 1)


@app.command()
def status(ctx: typer.Context,
           config: Path = typer.Argument(..., help="Test card or test catalog YAML.", metavar="<card_or_catalog>"),
           profile: str = typer.Option("acceptance", "--profile", help="Gate profile to check.", metavar="<name>"),
           env: Optional[str] = typer.Option(None, "--env", help="Require a report from this registered environment.", metavar="<name>"),
           project: Optional[str] = typer.Option(None, "--project", help="Select one catalog card.", metavar="<id>"),
           root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>")):
    """Tell whether an accepted report covers the current source; read only."""
    from .cli import emit
    from . import testing_run
    value = testing_run.status(ctx.obj.home, config, profile=profile, environment_name=env, root=root, project=project)
    emit(ctx, "test status", ok=value["accepted"], **value)
    raise typer.Exit(0 if value["accepted"] else 1)


@app.command()
def history(ctx: typer.Context,
            suite: Optional[str] = typer.Option(None, "--suite", help="Filter by card ID.", metavar="<id>"),
            project: Optional[str] = typer.Option(None, "--project", help="Filter by catalog card ID.", metavar="<id>"),
            profile: Optional[str] = typer.Option(None, "--profile", help="Filter by profile.", metavar="<name>"),
            group: Optional[str] = typer.Option(None, "--group", help="Filter by selected group or check ID.", metavar="<id>"),
            case: Optional[str] = typer.Option(None, "--case", help="Show results of test cases whose node ID contains this text.", metavar="<text>")):
    """List jforge test runs, newest first."""
    from .cli import emit
    from . import testing_run
    emit(ctx, "test history", runs=testing_run.history(ctx.obj.home, suite=suite, project=project, profile=profile,
                                                       group=group, case=case), case=case)


@app.command()
def report(ctx: typer.Context,
           run_or_acceptance: str = typer.Argument(..., help="Run ID or acceptance ID.", metavar="<run_id_or_acceptance_id>"),
           output_format: str = typer.Option("text", "--format", help="Output text, json or markdown.", metavar="<text|json|markdown>")):
    """Print the report of one run or one acceptance; run no tests."""
    from .cli import emit
    from . import testing_run
    from .storage import ForgeError
    if output_format not in {"text", "json", "markdown"}:
        raise ForgeError("--format takes text, json or markdown.")
    value = testing_run.report(ctx.obj.home, run_or_acceptance)
    if output_format == "markdown" and not ctx.obj.as_json:
        typer.echo(value["markdown"], nl=False)
        return
    emit(ctx, "test report", kind=value["kind"], report=value["report"],
         **({"markdown": value["markdown"]} if output_format == "text" and not ctx.obj.as_json else {}))
