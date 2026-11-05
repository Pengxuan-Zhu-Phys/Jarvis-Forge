"""Project build CLI; package operations remain owned by apt/Spack."""

from pathlib import Path
from typing import Optional

import typer
import yaml

from . import build_catalog as catalog
from . import builds
from .storage import ForgeError

app = typer.Typer(help="Build projects and compose verified build outputs.",
                  context_settings={"help_option_names": ["-h", "--help"]})


@app.command("list")
def list_builds(ctx: typer.Context):
    """List cached Release builds without network access."""
    from .cli import emit
    emit(ctx, "build list", builds=catalog.entries(ctx.obj.home), libraries=catalog.libraries(ctx.obj.home))


@app.command()
def update(ctx: typer.Context, source: str = typer.Option(catalog.INDEX_URL, "--source", help="Build index HTTPS URL or local file URI.")):
    """Download and validate an independent build Release."""
    from .cli import emit
    emit(ctx, "build update", **catalog.update(ctx.obj.home, source))


@app.command()
def show(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>"),
         expanded: bool = typer.Option(False, "--expanded", help="Show the effective execution card including defaults.")):
    """Show one local or cached build without executing it."""
    from .cli import emit
    source = catalog.resolve(ctx.obj.home, build)
    if not expanded:
        from .suite import UniqueLoader
        from .storage import digest
        value = yaml.load(source.read_text(), Loader=UniqueLoader)
        if not isinstance(value, dict):
            raise ForgeError("Expected a build card mapping.")
        if ctx.obj.as_json:
            emit(ctx, "build show", path=str(source), sha256=digest(source), build=value)
        else:
            typer.echo(yaml.safe_dump(value, sort_keys=False), nl=False)
        return
    value = builds.load(source, home=ctx.obj.home)
    if ctx.obj.as_json:
        emit(ctx, "build show", path=value["path"], sha256=value["sha256"], build=value["config"])
    else:
        typer.echo(yaml.safe_dump(value["config"], sort_keys=False), nl=False)


@app.command()
def fetch(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>"),
          output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write an editable YAML without overwriting a file."),
          directory: Optional[Path] = typer.Option(None, "--directory", help="Download an editable workspace with sources/build/install kept inside it.")):
    """Fetch a Release recipe and scripts, optionally as a complete workspace."""
    from .cli import emit
    if (output is None) == (directory is None):
        raise ForgeError("Select exactly one of --output or --directory.")
    source = catalog.acquire(ctx.obj.home, build)
    if directory is not None:
        from .build_workspace import create
        path = create(ctx.obj.home, source, directory)
        emit(ctx, "build fetch", path=str(path), workspace=str(path.parent))
    else:
        path = catalog.copy_bundle(source, output, home=ctx.obj.home)
        emit(ctx, "build fetch", path=str(path))


def parameters(values):
    result = {}
    for item in values:
        if "=" not in item:
            raise ForgeError("Use --set NAME=VALUE.")
        key, text = item.split("=", 1)
        value = yaml.safe_load(text)
        if not key or not isinstance(value, (str, int, float, bool)):
            raise ForgeError("Build parameter values must be scalar strings, numbers or booleans.")
        result[key] = value
    return result


@app.command()
def validate(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>")):
    """Validate the build schema and cross-build dependency graph."""
    from .cli import emit
    value = builds.graph(ctx.obj.home, build)
    emit(ctx, "build validate", build_id=value["nodes"][value["root"]]["config"]["id"], nodes=len(value["nodes"]))


@app.command()
def plan(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>"),
         env: Optional[str] = typer.Option(None, "--env", help="Display the selected registered environment name."),
         setting: list[str] = typer.Option([], "--set", help="Override a declared parameter: NAME=VALUE.")):
    """Inspect a dependency graph without installs or CMake execution."""
    from .cli import emit
    emit(ctx, "build plan", **builds.plan(ctx.obj.home, build, parameters=parameters(setting), environment_name=env))


def _execute(ctx, build=None, setting=(), env=None, jobs=1, resume=None, directory=None):
    from .cli import emit, line, _console
    if not resume:
        if directory is not None:
            from .build_workspace import create, locate
            workspace = directory.expanduser().resolve()
            if locate(workspace):
                entry = workspace / "build.yaml"
                if build != builds.load(entry, home=ctx.obj.home)["config"]["id"] and Path(build).expanduser().resolve() != entry:
                    raise ForgeError(f"Workspace contains a different build: {workspace}")
                build = str(entry)
            else:
                build = str(create(ctx.obj.home, catalog.acquire(ctx.obj.home, build), workspace))
        else:
            build = str(catalog.acquire(ctx.obj.home, build))
    result = builds.execute(ctx.obj.home, build, parameters=parameters(setting), environment_name=env,
                               jobs=jobs, resume=resume, progress=lambda message: line(_console(stderr=True), message))
    emit(ctx, "build resume" if resume else "build run", ok=result["status"] == "passed", **builds.summary(result))
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)


@app.command()
def run(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>"),
        setting: list[str] = typer.Option([], "--set", help="Override a declared parameter: NAME=VALUE."),
        env: Optional[str] = typer.Option(None, "--env", help="Use this registered compiler/Python environment."),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Maximum concurrent build jobs."),
        directory: Optional[Path] = typer.Option(None, "--directory", help="Fetch and build in this workspace; reuse its editable recipe on later runs.")):
    """Execute projects, dependencies and acceptance checks with live logs."""
    _execute(ctx, build, setting, env, jobs, directory=directory)


@app.command()
def resume(ctx: typer.Context, run_id: str = typer.Argument(..., metavar="<run_id>"),
           jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Maximum concurrent build jobs.")):
    """Retry a saved graph with its original recipe and environment snapshots."""
    _execute(ctx, jobs=jobs, resume=run_id)


@app.command("artifacts")
def artifacts(ctx: typer.Context):
    """List recorded cross-build installation outputs."""
    from .cli import emit
    from .storage import read_json
    rows = []
    for path in sorted((ctx.obj.home.root / "builds/artifacts").glob("*/manifest.json")):
        value = read_json(path)
        rows.append({k: value[k] for k in ("artifact_id", "build_id", "output", "prefix", "version", "status")})
    emit(ctx, "build artifacts", artifacts=rows)


@app.command("env")
def artifact_environment(ctx: typer.Context, artifact_id: str = typer.Argument(..., metavar="<artifact_id>"),
                         path: bool = typer.Option(False, "--path", help="Print only the verified activation script path.")):
    """Locate the runtime environment exported by a verified installation."""
    from .cli import emit
    value = builds.artifact(ctx.obj.home, artifact_id)
    if path:
        if ctx.obj.as_json:
            raise ForgeError("--path and --json are mutually exclusive.")
        print(value["activation"])
    else:
        emit(ctx, "build env", artifact_id=artifact_id, activation=value["activation"])
