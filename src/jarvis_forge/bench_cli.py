"""jforge bench: run case batches and compare numeric results.

Design: Jarvis-Books Docs/Jarvis-Forge/BENCH_DESIGN.md.
"""

from pathlib import Path
from typing import Optional

import typer

from . import bench, bench_project
from .storage import ForgeError

app = typer.Typer(help="Run benchmark cards, reuse finished steps, and compare numbers.",
                  context_settings={"help_option_names": ["-h", "--help"]})


def _echo(ctx, command, result, *, ok=True, exit_code=0):
    from .cli import emit
    if ctx.obj.as_json:
        emit(ctx, command, ok=ok, **{key: value for key, value in result.items() if key != "text"})
    else:
        typer.echo(result.get("text") or "", nl=False)
    if exit_code:
        raise typer.Exit(exit_code)


@app.command()
def init(ctx: typer.Context, directory: Path = typer.Argument(Path("."), help="Project directory.", metavar="<dir>")):
    """Create a bench project in a directory."""
    _echo(ctx, "bench init", bench_project.initialize(directory))


@app.command()
def fetch(ctx: typer.Context, project: str = typer.Argument(..., help="Official project name.", metavar="<name>")):
    """Fetch a verified benchmark project and its data into the current workspace."""
    _echo(ctx, "bench fetch", bench_project.fetch(ctx.obj.home, project))


@app.command()
def validate(ctx: typer.Context, card: Optional[str] = typer.Argument(None, help="Bench card or registered entrypoint.", metavar="<card>")):
    """Check the card, cases and reference files without running commands."""
    _echo(ctx, "bench validate", bench.validate(bench_project.resolve_card(card)))


@app.command()
def plan(ctx: typer.Context,
         card: Optional[str] = typer.Argument(None, help="Bench card or registered entrypoint.", metavar="<card>"),
         profile: Optional[str] = typer.Option(None, "--profile", help="Select a named profile.", metavar="<name>"),
         case: list[str] = typer.Option([], "--case", help="Select case names or globs.", metavar="<id>"),
         tag: list[str] = typer.Option([], "--tag", help="Select cases with any of these tags.", metavar="<tag>"),
         redo: list[str] = typer.Option([], "--redo", help="Plan this step as if reuse were off.", metavar="<step>"),
         keep: list[str] = typer.Option([], "--keep", help="Plan to keep old results when inputs differ.", metavar="<step>"),
         env: Optional[str] = typer.Option(None, "--env", help="Use this registered environment.", metavar="<name>"),
         executor: str = typer.Option("local", "--executor", help="Plan against this executor's data directory.", metavar="<name>")):
    """Show which software and tasks would run. Do not run them."""
    result = bench.plan(ctx.obj.home, bench_project.resolve_card(card), profile=profile, cases=case, tags=tag, redo=redo, keep=keep,
                        environment_name=env, executor=executor)
    _echo(ctx, "bench plan", result)


@app.command()
def prefetch(ctx: typer.Context, card: Optional[str] = typer.Argument(None, help="Bench card or registered entrypoint.", metavar="<card>")):
    """Download fetch items into the cache. Unpack when the card asks for it."""
    from . import bench_exec
    _echo(ctx, "bench prefetch", bench_exec.prefetch(ctx.obj.home, bench_project.resolve_card(card)))


@app.command()
def run(ctx: typer.Context,
        card: Optional[str] = typer.Argument(None, help="Bench card or registered entrypoint.", metavar="<card>"),
        profile: Optional[str] = typer.Option(None, "--profile", help="Select a named profile.", metavar="<name>"),
        case: list[str] = typer.Option([], "--case", help="Select case names or globs.", metavar="<id>"),
        tag: list[str] = typer.Option([], "--tag", help="Select cases with any of these tags.", metavar="<tag>"),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Concurrent local tasks, counted in cores.", metavar="<n>"),
        no_build: bool = typer.Option(False, "--no-build", help="Reuse installed software and do not build."),
        redo: list[str] = typer.Option([], "--redo", help="Run this step again and replace its results.", metavar="<step>"),
        keep: list[str] = typer.Option([], "--keep", help="Keep old results when inputs differ.", metavar="<step>"),
        verify: Optional[str] = typer.Option(None, "--verify", help="Recheck output checksums. Only 'full' is accepted.", metavar="<full>"),
        executor: str = typer.Option("local", "--executor", help="Run tasks with this executor.", metavar="<name>"),
        detach: bool = typer.Option(False, "--detach", help="Submit the first Slurm wave and return."),
        env: Optional[str] = typer.Option(None, "--env", help="Use this registered environment.", metavar="<name>"),
        baseline: Optional[str] = typer.Option(None, "--baseline", help="Compare expected: baseline against this run.", metavar="<run_id>"),
        rerun_failed: Optional[str] = typer.Option(None, "--rerun-failed", help="Run only the failed cases of this run. The new run is not gate evidence.", metavar="<run_id>")):
    """Run the selected cases. Reuse finished steps whose inputs still match."""
    from .cli import line, _console
    if verify not in {None, "full"}:
        raise ForgeError("--verify takes full.")
    result = bench.run(ctx.obj.home, bench_project.resolve_card(card), profile=profile, cases=case, tags=tag, jobs=jobs, no_build=no_build,
                       redo=redo, keep=keep, verify_full=verify == "full", environment_name=env, executor=executor,
                       baseline=baseline, rerun_failed=rerun_failed, detach=detach,
                       progress=lambda text: line(_console(stderr=True), text))
    code = 0 if result.get("detached") or result["status"] in {"passed", "reported"} else 1
    _echo(ctx, "bench run", result, ok=code == 0, exit_code=code)


@app.command()
def verify(ctx: typer.Context,
           card: Path = typer.Argument(..., help="Bench card.", metavar="<card>"),
           step: str = typer.Option(..., "--step", help="Re-run this step's verify command.", metavar="<step>"),
           profile: Optional[str] = typer.Option(None, "--profile", help="Select a named profile.", metavar="<name>"),
           case: list[str] = typer.Option([], "--case", help="Select case names or globs.", metavar="<id>"),
           env: Optional[str] = typer.Option(None, "--env", help="Use this registered environment.", metavar="<name>")):
    """Re-run verify on finished results. Failures are generated again on the next run."""
    result = bench.verify(ctx.obj.home, bench_project.resolve_card(card), step=step, profile=profile, cases=case, environment_name=env)
    result["text"] = f"{result['step']}: checked {result['checked']}, failed {result['failed']}\n"
    _echo(ctx, "bench verify", result, ok=result["failed"] == 0, exit_code=0 if result["failed"] == 0 else 1)


@app.command()
def status(ctx: typer.Context,
           target: Optional[str] = typer.Argument(None, help="Run ID or bench card. Defaults to the project main card.", metavar="<run_or_card>"),
           profile: Optional[str] = typer.Option(None, "--profile", help="Read the gate for this profile of a card.", metavar="<name>"),
           env: Optional[str] = typer.Option(None, "--env", help="Require this registered environment.", metavar="<name>")):
    """Show one run, or tell whether a card profile meets its gate."""
    if target is None or profile or target.endswith((".yaml", ".yml")) or Path(target).expanduser().exists():
        result = bench.card_status(ctx.obj.home, bench_project.resolve_card(target), profile=profile, environment_name=env)
        _echo(ctx, "bench status", result, ok=result["accepted"], exit_code=0 if result["accepted"] else 1)
        return
    result = bench.status(ctx.obj.home, target)
    code = 0 if result["status"] in {"passed", "reported", "running"} else 1
    _echo(ctx, "bench status", result, ok=code == 0, exit_code=code)


@app.command()
def watch(ctx: typer.Context, run_id: str = typer.Argument(..., help="Bench run ID.", metavar="<run_id>")):
    """Reconcile a run until it finishes. Ctrl+C stops watching and leaves the jobs running."""
    from . import bench_exec
    result = bench_exec.watch(ctx.obj.home, run_id)
    if result.get("interrupted"):
        raise KeyboardInterrupt
    code = 0 if result["status"] in {"passed", "reported"} else 1
    _echo(ctx, "bench watch", result, ok=code == 0, exit_code=code)


@app.command()
def cancel(ctx: typer.Context,
           run_id: str = typer.Argument(..., help="Bench run ID.", metavar="<run_id>"),
           yes: bool = typer.Option(False, "--yes", help="Cancel the run's jobs.")):
    """Cancel a running bench's Slurm jobs and mark the run cancelled."""
    from . import bench_exec
    result = bench_exec.cancel(ctx.obj.home, run_id, yes=yes)
    _echo(ctx, "bench cancel", result)


@app.command("exec-task", hidden=True)
def exec_task(run_dir: Path = typer.Argument(..., help="Bench run directory."),
              task: str = typer.Argument(..., help="case/step/index")):
    """Run one array task. Slurm job scripts call this."""
    from . import bench_exec
    bench_exec.exec_task(run_dir, task)


@app.command()
def resume(ctx: typer.Context,
           run_id: str = typer.Argument(..., help="Bench run ID.", metavar="<run_id>"),
           retry_failed: bool = typer.Option(False, "--retry-failed", help="Run failed tasks again in this run."),
           jobs: Optional[int] = typer.Option(None, "--jobs", "-j", min=1, help="Concurrent local tasks.", metavar="<n>")):
    """Continue a bench run from the results already on disk."""
    from .cli import line, _console
    result = bench.resume(ctx.obj.home, run_id, retry_failed=retry_failed, jobs=jobs,
                          progress=lambda text: line(_console(stderr=True), text))
    code = 0 if result["status"] in {"passed", "reported"} else 1
    _echo(ctx, "bench resume", result, ok=code == 0, exit_code=code)


@app.command()
def report(ctx: typer.Context,
           run_id: str = typer.Argument(..., help="Bench run ID.", metavar="<run_id>"),
           case: Optional[str] = typer.Option(None, "--case", help="Show one case.", metavar="<id>"),
           output_format: str = typer.Option("md", "--format", help="md, json or csv.", metavar="<md|json|csv>")):
    """Print the report written by a bench run."""
    from .cli import emit
    if output_format not in {"md", "json", "csv"}:
        raise ForgeError("--format takes md, json or csv.")
    result = bench.report(ctx.obj.home, run_id, case=case)
    if output_format == "md" and not ctx.obj.as_json:
        typer.echo(result["markdown"], nl=False)
        return
    if output_format == "csv" and not ctx.obj.as_json:
        payload = result.get("report")
        rows = payload["cases"] if payload else [result["case"]]
        typer.echo("case,status")
        for item in rows:
            typer.echo(f"{item['name']},{item['status']}")
        return
    emit(ctx, "bench report", **{key: value for key, value in result.items() if key != "markdown"})


@app.command()
def compare(ctx: typer.Context,
            run_a: str = typer.Argument(..., help="Earlier bench run.", metavar="<run_a>"),
            run_b: str = typer.Argument(..., help="Later bench run.", metavar="<run_b>")):
    """Align two runs by case and item, after checking that the inputs still match."""
    result = bench.compare_runs(ctx.obj.home, run_a, run_b)
    _echo(ctx, "bench compare", result, ok=result["comparable"], exit_code=0 if result["comparable"] else 2)


@app.command()
def trend(ctx: typer.Context,
          card: Path = typer.Argument(..., help="Bench card.", metavar="<card>"),
          case: str = typer.Option(..., "--case", help="Case name.", metavar="<id>"),
          step: str = typer.Option(..., "--step", help="Compare step.", metavar="<step>"),
          item: str = typer.Option(..., "--item", help="Item name inside that step.", metavar="<name>"),
          output_format: str = typer.Option("csv", "--format", help="csv or json.", metavar="<csv|json>")):
    """Show one item across finished runs, oldest first, with the subject software id."""
    from .cli import emit
    if output_format not in {"csv", "json"}:
        raise ForgeError("--format takes csv or json.")
    result = bench.trend(ctx.obj.home, bench_project.resolve_card(card), case=case, step=step, item=item)
    if output_format == "json" or ctx.obj.as_json:
        emit(ctx, "bench trend", **{key: value for key, value in result.items() if key != "text"})
        return
    typer.echo(result["text"], nl=False)


data_app = typer.Typer(help="Show or clean stored bench results.")
app.add_typer(data_app, name="data")


@data_app.command("list")
def data_list(ctx: typer.Context,
              card: Optional[str] = typer.Argument(None, help="Card name or path. Omit to list every card.", metavar="[card]")):
    """List the bytes held by each card, profile, case and step."""
    _echo(ctx, "bench data list", bench.data_list(ctx.obj.home, card))


@data_app.command("prune")
def data_prune(ctx: typer.Context,
               card: Optional[str] = typer.Argument(None, help="Card path. A path also drops profiles and steps the card no longer names.", metavar="[card]"),
               yes: bool = typer.Option(False, "--yes", help="Delete the leftover directories.")):
    """Delete .partial and .new directories. With a card path, also delete unknown profiles, cases and steps."""
    _echo(ctx, "bench data prune", bench.data_prune(ctx.obj.home, card, yes=yes))


@app.command()
def history(ctx: typer.Context,
            card: Optional[str] = typer.Option(None, "--card", help="Filter by card name.", metavar="<name>"),
            case: Optional[str] = typer.Option(None, "--case", help="Filter by case name.", metavar="<id>"),
            profile: Optional[str] = typer.Option(None, "--profile", help="Filter by profile.", metavar="<name>")):
    """List bench runs, newest first."""
    from .cli import emit
    rows = bench.history(ctx.obj.home, card=card, case=case, profile=profile)
    if ctx.obj.as_json:
        emit(ctx, "bench history", runs=rows)
        return
    if not rows:
        typer.echo("No bench runs.")
        return
    for item in rows:
        typer.echo(f"{item['at']}  {item['run_id']}  {item['name']}  {item['profile']}  {item['status']}")


from .executor_cli import app as executor_app

app.add_typer(executor_app, name="executor")
