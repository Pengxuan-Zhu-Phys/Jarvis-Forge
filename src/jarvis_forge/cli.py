"""CLI owned by Forge; hosts can call main(argv, prog='Jarvis forge')."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
import traceback
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer

from . import __version__
from .comparison import compare
from .environment import inspect_environment
from .environment_registry import show as registered_environment
from .runner import execute
from .storage import ForgeError, ForgeHome, default_home_file, identifier, read_json
from .suite import load_suite, make_plan


app = typer.Typer(add_completion=False, pretty_exceptions_enable=False,
                  context_settings={"help_option_names": ["-h", "--help"]},
                  help="Prepare user environments and preserve command acceptance evidence.\n\n"
                       "Command help: jforge COMMAND -h")


@dataclass
class Settings:
    home: ForgeHome
    as_json: bool


def _console(stderr=False) -> Console:
    return Console(stderr=stderr, no_color=bool(os.environ.get("NO_COLOR")), highlight=False)


def emit(ctx: typer.Context, command: str, *, ok=True, **values):
    result = {"ok": ok, "command": command, **values}
    if not ok and not isinstance(result.get("error"), dict):
        result["error"] = {"type": "VerificationFailed", "hint": None,
                           "message": str(result.get("error") or
                                          f"{command} completed with status {result.get('status', 'failed')}.")}
    if ctx.obj.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        console = _console()
        for key, value in values.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                columns = [k for k in ("name", "version", "install_id", "prefix", "run_id", "suite_id",
                                      "status", "active", "automatic", "action", "id", "before", "after", "command_changed", "revision", "python", "python_version", "path", "description")
                           if k in value[0]]
                if columns:
                    table = Table(title=key.replace("_", " ").title(), box=None)
                    for column in columns:
                        table.add_column(column)
                    for item in value:
                        table.add_row(*(str(item.get(k, "")) for k in columns))
                    console.print(table)
                    continue
            console.print(f"{key}: " + (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)),
                          markup=False)
    return result


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context,
         home: Optional[str] = typer.Option(None, "--home", help="Use this Forge user directory; init remembers it.", metavar="<path>"),
         as_json: bool = typer.Option(False, "--json", help="Output one JSON object."),
         version: bool = typer.Option(False, "--version", "-v", help="Show the version and exit.", is_eager=True),
         debug: bool = typer.Option(False, "--debug", "-d", help="Show diagnostic tracebacks.")):
    # Version queries must remain available even if a saved preference is corrupt.
    ctx.obj = Settings(ForgeHome.resolve(home, read_default=not version), as_json)
    if version:
        if as_json:
            emit(ctx, "version", name="Jarvis-Forge", version=__version__)
        else:
            print(f"Jarvis-Forge {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if as_json:
            emit(ctx, "help", commands=sorted(ctx.command.commands))
        else:
            typer.echo(ctx.get_help())


@app.command()
def init(ctx: typer.Context):
    """Initialize user directories and remember this default location."""
    config = ctx.obj.home.initialize()
    preference = ctx.obj.home.remember_default()
    emit(ctx, "init", **ctx.obj.home.paths(), host_id=config["host_id"],
         default_saved=True, default_home_file=str(preference))


@app.command()
def paths(ctx: typer.Context):
    """Show resolved paths without creating directories."""
    emit(ctx, "paths", **ctx.obj.home.paths(), default_home_file=str(default_home_file()))


@app.command()
def doctor(ctx: typer.Context,
           env: Optional[str] = typer.Option(None, "--env", help="Inspect this registered environment.", metavar="<name>"),
           shell: Optional[Path] = typer.Option(None, "--shell", help="Source this user-selected environment script.", metavar="<path>"),
           timeout: float = typer.Option(60, "--timeout", min=0.1, help="Limit activation and inspection time.", metavar="<seconds>")):
    """Report the user's active compilers and Python without selecting an environment."""
    if shell is not None and env is not None:
        raise ForgeError("--shell and --env are mutually exclusive.")
    profile = registered_environment(ctx.obj.home, env, strict=True) if env else None
    if profile:
        shell = Path(profile["activation"])
    environment = inspect_environment(shell, timeout)
    if profile:
        environment["environment_profile"] = profile
    if ctx.obj.as_json:
        emit(ctx, "doctor", system=platform.system(), machine=platform.machine(),
             python=sys.executable, python_version=platform.python_version(),
             tools=environment["tools"], environment=environment)
        return
    console = _console()
    console.print(f"Controller Python: {sys.executable} ({platform.python_version()})", markup=False)
    console.print(f"Environment script: {environment['environment_shell']['path'] if shell else '(inherited environment)'}", markup=False)
    table = Table("Tool", "Path", "Version / status", box=None)
    for name, tool in {**environment["compilers"], **environment["tool_details"]}.items():
        table.add_row(name, tool.get("path") or "—", tool.get("version") or tool["status"])
    console.print(table)
    for name, target in environment["pythons"].items():
        if target["status"] == "found":
            console.print(f"{name}: {target['executable']} ({target['version']}); prefix={target['prefix']}", markup=False)
            packages = {item["name"].lower(): item["version"] for item in target["packages"]}
            console.print("  " + "; ".join(f"{key}={packages.get(key, 'missing')}" for key in ("pyyaml", "pybind11")), markup=False)
        else:
            console.print(f"{name}: {target['status']}", markup=False)
    for key in ("PYENV_ROOT", "PYENV_VERSION", "VIRTUAL_ENV", "LOADEDMODULES"):
        console.print(f"{key}: {environment['variables'].get(key, '(unset)')}", markup=False)
    if environment["pyenv"].get("selection"):
        console.print(f"pyenv selection: {environment['pyenv']['selection']}", markup=False)
    for stream, content in environment["activation_output"].items():
        if content:
            _console(stderr=True).print(f"Activation {stream}:\n{content}", markup=False)


@app.command("list")
def list_packages(ctx: typer.Context):
    """List recorded software installations."""
    emit(ctx, "list", packages=ctx.obj.home.packages())


@app.command()
def info(ctx: typer.Context,
         package: str = typer.Argument(..., help="Package name.", metavar="<package>"),
         version: Optional[str] = typer.Option(None, "--package-version", help="Select a recorded software version.", metavar="<str>"),
         instance: Optional[str] = typer.Option(None, "--instance", help="Select an exact install ID.", metavar="<str>"),
         path: bool = typer.Option(False, "--path", help="Print only the unique installation prefix.")):
    """Locate recorded software versions and installation prefixes."""
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    records = [item for item in ctx.obj.home.packages(package)
               if (version is None or item["version"] == version)
               and (instance is None or item["install_id"] == instance)]
    if not records:
        raise ForgeError(f"No recorded installation matches {package!r}.", hint="jforge list")
    if path:
        if len(records) != 1:
            raise ForgeError(f"{package!r} has {len(records)} matching instances.",
                             hint=f"jforge info {package} --instance <install-id> --path")
        print(records[0]["prefix"])
    else:
        emit(ctx, "info", packages=records)


@app.command()
def example(ctx: typer.Context,
            output: Path = typer.Option(..., "--output", "-o", help="Write the bundled smoke suite.", metavar="<path>")):
    """Write a small runnable acceptance suite without overwriting files."""
    content = files("jarvis_forge").joinpath("examples/smoke.yaml").read_text()
    output = output.expanduser().resolve()
    if output.exists():
        raise ForgeError(f"Output already exists: {output}")
    output.write_text(content, encoding="utf-8")
    emit(ctx, "example", path=str(output))


@app.command()
def validate(ctx: typer.Context,
             suite: Path = typer.Argument(..., help="Acceptance YAML file.", metavar="<suite_yaml>"),
             env: Optional[str] = typer.Option(None, "--env", help="Explicitly select a registered environment.", metavar="<name>")):
    """Validate schema, dependencies, input paths and placeholders."""
    loaded = load_suite(suite)
    plan = make_plan(loaded, ctx.obj.home.root / "runs" / "planned-run", ctx.obj.home, env)
    emit(ctx, "validate", suite_id=plan["suite_id"], steps=len(plan["steps"]), path=loaded["path"])


@app.command()
def plan(ctx: typer.Context,
         suite: Path = typer.Argument(..., help="Acceptance YAML file.", metavar="<suite_yaml>"),
        env: Optional[str] = typer.Option(None, "--env", help="Explicitly select a registered environment.", metavar="<name>")):
    """Show a plan without executing commands or creating user state."""
    emit(ctx, "plan", **make_plan(load_suite(suite), ctx.obj.home.root / "runs" / "planned-run", ctx.obj.home, env))


@app.command()
def run(ctx: typer.Context,
        suite: Path = typer.Argument(..., help="Acceptance YAML file.", metavar="<suite_yaml>"),
        env: Optional[str] = typer.Option(None, "--env", help="Explicitly select a registered environment.", metavar="<name>")):
    """Execute every configured step and preserve a new run record."""
    def progress(event):
        suffix = f" {event['step_id']}" if "step_id" in event else ""
        if "status" in event:
            suffix += f" {event['status']}"
        _console(stderr=True).print(f"[{event['run_id']}] {event['event']}{suffix}", markup=False)

    result = execute(ctx.obj.home, suite, progress=progress, environment_name=env)
    emit(ctx, "run", ok=result["status"] == "passed", **result)
    raise typer.Exit(0 if result["status"] == "passed" else 130 if result["status"] == "interrupted" else 1)


@app.command()
def history(ctx: typer.Context,
            suite: Optional[str] = typer.Option(None, "--suite", help="Filter by stable suite ID.", metavar="<str>")):
    """List all runs, including failed or unfinished runs."""
    emit(ctx, "history", runs=ctx.obj.home.history(suite))


@app.command()
def show(ctx: typer.Context,
         run_id: str = typer.Argument(..., help="Exact run ID.", metavar="<run_id>"),
         step: Optional[str] = typer.Option(None, "--step", help="Show one step record.", metavar="<str>")):
    """Show a run or one completed step."""
    result = ctx.obj.home.show(run_id)
    if step is not None:
        identifier(step)
        record = ctx.obj.home.run_path(run_id) / "steps" / step / "result.json"
        if not record.exists():
            raise ForgeError(f"No completed step record for {step!r}.")
        emit(ctx, "show", run_id=run_id, step=read_json(record))
    else:
        emit(ctx, "show", **result)


@app.command()
def logs(ctx: typer.Context,
         run_id: str = typer.Argument(..., help="Exact run ID.", metavar="<run_id>"),
         step: str = typer.Option(..., "--step", help="Step ID.", metavar="<str>"),
         stream: str = typer.Option("stdout", "--stream", help="Select stdout or stderr.", metavar="<stdout|stderr>"),
         follow: bool = typer.Option(False, "--follow", help="Follow until the run has a final record.")):
    """Read one archived stdout or stderr stream."""
    if stream not in {"stdout", "stderr"} or (follow and ctx.obj.as_json):
        raise ForgeError("Use --stream stdout|stderr; --follow and --json are mutually exclusive.")
    identifier(step)
    run_dir = ctx.obj.home.run_path(run_id)
    path = run_dir / "steps" / step / f"{stream}.log"
    if not path.is_file():
        raise ForgeError(f"Log is not available: {path}")
    if ctx.obj.as_json:
        emit(ctx, "logs", run_id=run_id, step=step, stream=stream,
             text=path.read_text(encoding="utf-8", errors="replace"))
        return
    with path.open("r", encoding="utf-8", errors="replace") as log:
        while True:
            text = log.read()
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
            if not follow or (run_dir / "result.json").exists():
                break
            time.sleep(0.2)


@app.command("compare")
def compare_runs(ctx: typer.Context,
                 older: str = typer.Argument(..., help="Older run ID.", metavar="<old_run>"),
                 newer: str = typer.Argument(..., help="Newer run ID.", metavar="<new_run>"),
                 check: bool = typer.Option(False, "--check", help="Fail if evidence differs, is missing, or did not pass.")):
    """Compare archived evidence without executing tests."""
    result = compare(ctx.obj.home, older, newer)
    ok = result["check_passed"] if check else True
    emit(ctx, "compare", ok=ok, **result)
    if not ok:
        raise typer.Exit(1)


from .apt_cli import app as apt_app

app.add_typer(apt_app, name="apt")

from .env_cli import app as env_app

app.add_typer(env_app, name="env")


def _globals(argv: list[str]) -> list[str]:
    """Allow common options before/after commands, without parsing after '--'."""
    common, rest = [], []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            rest.extend(argv[index:])
            break
        if arg in {"--json", "--debug", "-d", "--version", "-v", "-V"}:
            common.append("--version" if arg == "-V" else arg)
        elif arg == "--home":
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise ForgeError("--home requires a directory path.")
            common.extend(argv[index:index + 2])
            index += 1
        elif arg.startswith("--home="):
            common.append(arg)
        else:
            rest.append(arg)
        index += 1
    return common + rest


def main(argv: Optional[list[str]] = None, prog: str = "jforge") -> int:
    """Public standalone/host entry point; no dependency on Jarvis-HEP."""
    args = list(sys.argv[1:] if argv is None else argv)
    options = args[:args.index("--")] if "--" in args else args
    as_json = "--json" in options
    debug = "--debug" in options or "-d" in options
    try:
        result = app(args=_globals(args), prog_name=prog, standalone_mode=False)
        return result if isinstance(result, int) else 0
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except (KeyboardInterrupt, click.Abort):
        exc = ForgeError("Interrupted.", code=130)
    except ForgeError as error:
        exc = error
    except click.ClickException as error:
        exc = ForgeError(error.format_message(), hint=f"{prog} -h")
    except OSError as error:
        exc = ForgeError(str(error), code=1)
    except Exception as error:
        if debug:
            traceback.print_exc()
        exc = ForgeError(f"Unexpected error: {error}", code=1)
    if as_json:
        print(json.dumps({"ok": False, "command": "error", "error": {
            "type": type(exc).__name__, "message": str(exc), "hint": exc.hint}}, indent=2))
    else:
        _console(stderr=True).print(Panel(str(exc) + (f"\nTry: {exc.hint}" if exc.hint else ""),
                                          title=prog, border_style="red"), markup=False)
    return exc.code

