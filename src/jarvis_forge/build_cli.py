"""Project build CLI; package operations remain owned by apt/Spack."""

import os
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
def list_builds(ctx: typer.Context,
                available: bool = typer.Option(False, "--available", help="Show cached build recipes and libraries.")):
    """List installed build outputs."""
    from .cli import emit
    if available:
        emit(ctx, "build list", view="available", builds=catalog.entries(ctx.obj.home),
             libraries=catalog.libraries(ctx.obj.home))
    else:
        emit(ctx, "build list", view="installed", builds=_artifacts(ctx.obj.home, installed_only=True), libraries=[])


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
        requirements = builds.host_requirements(value)
        if ctx.obj.as_json:
            emit(ctx, "build show", path=str(source), sha256=digest(source), build=value, host_requirements=requirements)
        else:
            show_build(_console(), value, path=source, sha256=digest(source), requirements=requirements)
        return
    value = builds.load(source, home=ctx.obj.home)
    requirements = builds.host_requirements(value["config"])
    if ctx.obj.as_json:
        emit(ctx, "build show", path=value["path"], sha256=value["sha256"], build=value["config"],
             host_requirements=requirements)
    else:
        show_build(_console(), value["raw_config"], path=value["path"], sha256=value["sha256"],
                   ordered=value["ordered"], requirements=requirements)


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
         setting: list[str] = typer.Option([], "--set", help="Override a declared parameter: NAME=VALUE."),
         source: Optional[Path] = typer.Option(None, "--source", help="Use this checkout instead of the card's local source.")):
    """Inspect a dependency graph without installs or CMake execution."""
    from .cli import emit
    emit(ctx, "build plan", **builds.plan(ctx.obj.home, build, parameters=parameters(setting), environment_name=env, source=source))


def _handoff_activation(result):
    from .build_workspace import activation_output

    selected = activation_output(result.get("outputs", {}))
    if not selected or not selected.get("activation"):
        return False
    # The interactive hook opens a private, per-invocation file on fd 9.
    # Keep stdout and the persisted build result independent of this handoff.
    with open(9, "wb", closefd=False) as handoff:
        handoff.write(os.fsencode(selected["activation"]) + b"\0")
    return True


def _execute(ctx, build=None, setting=(), env=None, jobs=1, resume=None, directory=None, no_activate=False, source=None):
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
    command_name = "build resume" if resume else "build run"
    try:
        result = builds.execute(ctx.obj.home, build, parameters=parameters(setting), environment_name=env,
                                jobs=jobs, resume=resume, source=source, verbose=ctx.obj.verbose, as_json=ctx.obj.as_json,
                                progress=lambda message: line(console, message))
    except ForgeError as exc:
        if not ctx.obj.as_json:
            raise
        emit(ctx, command_name, ok=False, error={
            "type": type(exc).__name__, "message": str(exc), "hint": exc.hint,
        })
        raise typer.Exit(exc.code) from exc
    values = builds.summary(result)
    passed = result["status"] == "passed"
    handed_off = False
    if passed and not ctx.obj.as_json and not no_activate and os.environ.get("_JFORGE_BUILD_ACTIVATION") == "1":
        try:
            handed_off = _handoff_activation(result)
        except OSError as exc:
            line(console, f"Build passed. Could not pass the activation path to the shell: {exc}")
    if ctx.obj.as_json:
        if not passed:
            values["error"] = {"type": "VerificationFailed", "message": values["error"],
                               "hint": values.pop("hint", None)}
        emit(ctx, command_name, ok=passed, **values)
    else:
        root = ctx.find_root()
        prog = root.info_name or "jforge"
        command = [prog]
        if root.params.get("home") is not None:
            command += ["--home", str(ctx.obj.home.root)]
        command += ["build", "resume", result["run_id"], "--jobs", str(jobs)]
        build_result(_console() if passed else console, values, prog, shlex.join(command), handed_off=handed_off)
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)


@app.command()
def run(ctx: typer.Context, build: str = typer.Argument(..., metavar="<build>"),
        setting: list[str] = typer.Option([], "--set", help="Override a declared parameter: NAME=VALUE."),
        env: Optional[str] = typer.Option(None, "--env", help="Use this registered compiler/Python environment."),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Maximum concurrent build jobs."),
        directory: Optional[Path] = typer.Option(None, "--directory", help="Fetch and build in this workspace; reuse its editable recipe on later runs."),
        source: Optional[Path] = typer.Option(None, "--source", help="Use this checkout instead of the card's local source."),
        no_activate: bool = typer.Option(False, "--no-activate", help="Skip activation in the calling interactive shell.")):
    """Build projects and run acceptance checks. Use --verbose for raw output."""
    _execute(ctx, build, setting, env, jobs, directory=directory, no_activate=no_activate, source=source)


@app.command()
def resume(ctx: typer.Context, run_id: str = typer.Argument(..., metavar="<run_id>"),
           jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Maximum concurrent build jobs."),
           no_activate: bool = typer.Option(False, "--no-activate", help="Skip activation in the calling interactive shell.")):
    """Retry a saved graph with its original recipe and environment snapshots."""
    _execute(ctx, jobs=jobs, resume=run_id, no_activate=no_activate)


def _artifacts(home, *, installed_only=False):
    from .storage import read_json

    rows = []
    for path in sorted((home.root / "builds/artifacts").glob("*/manifest.json")):
        value = read_json(path)
        if installed_only and (value["status"] != "ready" or not Path(value["prefix"]).is_dir()):
            continue
        rows.append({k: value[k] for k in ("artifact_id", "build_id", "output", "prefix", "version", "status")})
    return rows


@app.command("artifacts")
def artifacts(ctx: typer.Context):
    """List all build artifact records, including historical installations."""
    from .cli import emit

    emit(ctx, "build artifacts", artifacts=_artifacts(ctx.obj.home))


@app.command("env")
def artifact_environment(ctx: typer.Context, artifact_id: str = typer.Argument(..., metavar="<artifact_id>"),
                         path: bool = typer.Option(False, "--path", help="Print only the verified activation script path.")):
    """Locate the runtime environment exported by a verified installation."""
    from .cli import emit
    from .shell_repair import artifact_activation
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    value = builds.artifact(ctx.obj.home, artifact_id)
    activation = str(artifact_activation(ctx.obj.home, value))
    if path:
        print(activation)
    else:
        emit(ctx, "build env", artifact_id=artifact_id, activation=activation)
