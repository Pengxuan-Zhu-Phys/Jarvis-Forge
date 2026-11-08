"""Project build CLI; package operations remain owned by apt/Spack."""

from pathlib import Path
import shlex
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
    from .cli import emit, _console
    from .presentation import show_build
    from .storage import digest
    source = catalog.resolve(ctx.obj.home, build)
    if not expanded:
        from .suite import UniqueLoader
        value = yaml.load(source.read_text(), Loader=UniqueLoader)
        if not isinstance(value, dict):
            raise ForgeError("Expected a build card mapping.")
        if ctx.obj.as_json:
            emit(ctx, "build show", path=str(source), sha256=digest(source), build=value)
        else:
            show_build(_console(), value, path=source, sha256=digest(source))
        return
    value = builds.load(source, home=ctx.obj.home)
    if ctx.obj.as_json:
        emit(ctx, "build show", path=value["path"], sha256=value["sha256"], build=value["config"])
    else:
        show_build(_console(), value["raw_config"], path=value["path"], sha256=value["sha256"],
                   ordered=value["ordered"])


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
    from .presentation import build_result
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
    console = _console(stderr=True)
    result = builds.execute(ctx.obj.home, build, parameters=parameters(setting), environment_name=env,
                               jobs=jobs, resume=resume, verbose=ctx.obj.verbose, as_json=ctx.obj.as_json,
                               progress=lambda message: line(console, message))
    values = builds.summary(result)
    passed = result["status"] == "passed"
    if ctx.obj.as_json:
        if not passed:
            values["error"] = {"type": "VerificationFailed", "message": values["error"],
                               "hint": values.pop("hint", None)}
        emit(ctx, "build resume" if resume else "build run", ok=passed, **values)
    else:
        root = ctx.find_root()
        prog = root.info_name or "jforge"
        command = [prog]
        if root.params.get("home") is not None:
            command += ["--home", str(ctx.obj.home.root)]
        command += ["build", "resume", result["run_id"], "--jobs", str(jobs)]
        build_result(_console() if passed else console, values, prog, shlex.join(command))
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)


@app.command()
def run(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>"),
        setting: list[str] = typer.Option([], "--set", help="Override a declared parameter: NAME=VALUE."),
        env: Optional[str] = typer.Option(None, "--env", help="Use this registered compiler/Python environment."),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Maximum concurrent build jobs."),
        directory: Optional[Path] = typer.Option(None, "--directory", help="Fetch and build in this workspace; reuse its editable recipe on later runs.")):
    """Build projects and run acceptance checks. Use --verbose for raw output."""
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
    from .cmake import runtime_activation
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    value = builds.artifact(ctx.obj.home, artifact_id)
    activation = str(runtime_activation(Path(value["activation"])))
    if path:
        print(activation)
    else:
        emit(ctx, "build env", artifact_id=artifact_id, activation=activation)
