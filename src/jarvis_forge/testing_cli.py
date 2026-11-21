"""jforge test: validate, plan, run and summarize YAML test cards and catalogs over pytest.

Design: Jarvis-Books Docs/Jarvis-Forge/TEST_CATALOG_DESIGN.md.
"""

from pathlib import Path
from typing import Optional

import typer

from . import testing
from .storage import ForgeError

app = typer.Typer(help="Validate, plan, run and accept YAML test cards and catalogs over pytest.",
                  context_settings={"help_option_names": ["-h", "--help"]})


def _schema1(path):
    """Schema 1 checker cards stay on the checker engine. Schema 2 uses jforge test."""
    _, _, data = testing._read(path)
    return isinstance(data, dict) and data.get("kind") == "checker" and data.get("schema_version") == 1


@app.command("list")
def list_checks(ctx: typer.Context,
                config: Path = typer.Argument(..., help="Schema 1 checker card.", metavar="<card>")):
    """List schema 1 checks, inputs and prerequisites without executing them."""
    from .cli import emit
    from . import checker
    if not _schema1(config):
        raise ForgeError("Schema 2 cards are listed with jforge test plan.", hint=f"jforge test plan {config}")
    loaded = checker.load(config)
    emit(ctx, "test list", checker_id=loaded["config"]["id"], project=loaded["project"], checks=loaded["ordered"])


@app.command()
def validate(ctx: typer.Context,
             config: Path = typer.Argument(..., help="Test card or test catalog YAML.", metavar="<card_or_catalog>"),
             root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>")):
    """Check cards, profiles, coverage and gate rules without running tests."""
    from .cli import emit
    emit(ctx, "test validate", **testing.validate(config, root=root))


@app.command()
def plan(ctx: typer.Context,
         config: Optional[Path] = typer.Argument(None, help="Test card, catalog or project directory.", metavar="[target]"),
         profile: Optional[str] = typer.Option(None, "--profile", help="Select a named profile.", metavar="<name>"),
         group: list[str] = typer.Option([], "--group", help="Select group or check IDs and their prerequisites.", metavar="<id>"),
         batch: Optional[str] = typer.Option(None, "--batch", help="Show shard k of n for each pytest group.", metavar="<k/n>"),
         project: Optional[str] = typer.Option(None, "--project", help="Select one catalog card.", metavar="<id>"),
         root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>")):
    """Show selected steps, files and shards without running pytest or creating user state."""
    from .cli import emit
    emit(ctx, "test plan", **testing.plan(testing.project_card(config), profile=profile, groups=group, batch=batch,
                                         root=root, project=project))


@app.command()
def run(ctx: typer.Context,
        config: Optional[Path] = typer.Argument(None, help="Test card, catalog or project directory.", metavar="[target]"),
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
    config = testing.project_card(config)
    if _schema1(config):
        rejected = [name for name, value in (("--profile", profile), ("--batch", batch),
                                              ("--acceptance-id", acceptance_id), ("--rerun-failed", rerun_failed),
                                              ("--project", project), ("--root", root)) if value is not None]
        if rejected:
            raise ForgeError("Schema 1 cards do not take " + ", ".join(rejected) + ".",
                             hint="Use --group to select check IDs.")
        from . import checker
        result = checker.run(ctx.obj.home, config, full=full, selected=group, environment_name=env, jobs=jobs,
                             progress=lambda text: line(_console(stderr=True), text))
        emit(ctx, "test run", ok=result["status"] == "passed", **result)
        raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)
    from . import testing_run
    result = testing_run.run(ctx.obj.home, config, profile=profile, groups=group, batch=batch,
                             acceptance_id=acceptance_id, full=full, environment_name=env, root=root,
                             project=project, jobs=jobs, rerun_failed=rerun_failed,
                             progress=lambda text: line(_console(stderr=True), text))
    emit(ctx, "test run", ok=result["status"] == "passed", **result)
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)


@app.command()
def accept(ctx: typer.Context,
           config: Optional[Path] = typer.Argument(None, help="Test card, catalog or project directory.", metavar="[target]"),
           profile: str = typer.Option("acceptance", "--profile", help="Gate profile (acceptance or release)."),
           env: Optional[str] = typer.Option(None, "--env", help="Use this registered environment."),
           project: Optional[str] = typer.Option(None, "--project", help="Select one catalog card."),
           root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory."),
           full: bool = typer.Option(False, "--full", help="Run every selected step again."),
           jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Concurrent build jobs inside build checks.")):
    """Run a full gate and succeed only when the current project is accepted."""
    from .cli import _console, emit, line
    from . import testing_run
    target = testing.project_card(config)
    console = _console(stderr=True)
    if console.is_terminal and not ctx.obj.as_json:
        line(console, f"Running {profile} · {target.name}", style="bold")
    result = testing_run.accept(ctx.obj.home, target, profile=profile, full=full,
                                environment_name=env, root=root, project=project, jobs=jobs)
    interrupted = result["status"] == "interrupted"
    accepted = result.get("accepted", False)
    emit(ctx, "test accept", ok=accepted, **result)
    raise typer.Exit(130 if interrupted else 0 if accepted else 1)


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
           config: Optional[Path] = typer.Argument(None, help="Test card, catalog or project directory.", metavar="[target]"),
           profile: str = typer.Option("acceptance", "--profile", help="Gate profile to check.", metavar="<name>"),
           env: Optional[str] = typer.Option(None, "--env", help="Require a report from this registered environment.", metavar="<name>"),
           project: Optional[str] = typer.Option(None, "--project", help="Select one catalog card.", metavar="<id>"),
           root: Optional[Path] = typer.Option(None, "--root", help="Resolve catalog card paths under this directory.", metavar="<dir>")):
    """Tell whether an accepted report covers the current source; read only."""
    from .cli import emit
    from . import testing_run
    value = testing_run.status(ctx.obj.home, testing.project_card(config), profile=profile, environment_name=env,
                               root=root, project=project)
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
           output_format: str = typer.Option("text", "--format", help="Output text, json, markdown, or per-case html.", metavar="<text|json|markdown|html>"),
           case: Optional[str] = typer.Option(None, "--case", help="Read this new-contract instance ID.", metavar="<instance_id>"),
           rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild --case HTML from its persisted facts; preserve earlier presentation.")):
    """Print the report of one run or one acceptance; run no tests."""
    from .cli import emit
    from . import testing_run
    if output_format == "json":ctx.obj.as_json = True
    if case:
        from .test_reports import run_case
        if output_format not in {"text", "json", "html"}: raise ForgeError("--case takes --format json or html.")
        value = run_case(ctx.obj.home, run_or_acceptance, case, "html" if output_format == "html" else "json", rebuild=rebuild)
        if output_format == "html" and not ctx.obj.as_json: typer.echo(value, nl=False)
        else: emit(ctx, "test report", kind="test-case", report=value)
        return
    if rebuild: raise ForgeError("--rebuild requires --case <instance_id>.")
    if output_format not in {"text", "json", "markdown"}:
        raise ForgeError("--format takes text, json or markdown.")
    value = testing_run.report(ctx.obj.home, run_or_acceptance)
    if output_format == "markdown" and not ctx.obj.as_json:
        typer.echo(value["markdown"], nl=False)
        return
    emit(ctx, "test report", kind=value["kind"], report=value["report"],
         **({"markdown": value["markdown"]} if output_format == "text" and not ctx.obj.as_json else {}))


metadata_app = typer.Typer(help="Validate descriptions without importing tests; scaffold drafts for review.",
                           context_settings={"help_option_names": ["-h", "--help"]})
app.add_typer(metadata_app, name="metadata")


@metadata_app.command("validate")
def metadata_validate(ctx: typer.Context,
                      index: Path = typer.Argument(..., help="Metadata index YAML.", metavar="<index>"),
                      strict: bool = typer.Option(False, "--strict", help="Require unique complete bindings against --inventory from explicit discover."),
                      inventory: Optional[Path] = typer.Option(None, "--inventory", help="Inventory JSON returned by discover; never starts pytest.", metavar="<file>")):
    """Check metadata format, IDs, containment and optional discovered bindings."""
    from . import test_metadata
    from .storage import read_json
    from .cli import emit
    emit(ctx, "test metadata validate", **test_metadata.validate(index, strict=strict, inventory=read_json(inventory) if inventory else None))


@metadata_app.command("scaffold")
def metadata_scaffold(ctx: typer.Context,
                      target: Path = typer.Argument(..., help="Project directory or Python test file.", metavar="<target>"),
                      output: Path = typer.Option(..., "--output", help="New directory for AST-only review drafts; never overwritten.", metavar="<dir>")):
    """Write review drafts without importing tests or inventing reviewed criteria."""
    from . import test_metadata
    from .cli import emit
    emit(ctx, "test metadata scaffold", **test_metadata.scaffold(target, output))


@app.command("discover")
def discover(ctx: typer.Context,
             target: Optional[Path] = typer.Argument(None, help="Project directory or test card; imports tests explicitly.", metavar="[target]"),
             profile: Optional[str] = typer.Option(None, "--profile", help="Select the same profile as test run.", metavar="<name>"),
             group: list[str] = typer.Option([], "--group", help="Select groups/checks and their prerequisites.", metavar="<id>"),
             env: Optional[str] = typer.Option(None, "--env", help="Use this existing registered environment.", metavar="<name>")):
    """Collect actual selected items with their selected interpreter; run no test bodies."""
    from .test_discovery import discover_card
    from .cli import emit
    result = discover_card(ctx.obj.home, testing.project_card(target), profile=profile, groups=group, environment_name=env)
    emit(ctx, "test discover", ok=not result["errors"], **result)
    if result["errors"]: raise typer.Exit(1)
