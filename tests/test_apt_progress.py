"""Exercise the CLI and real subprocess pipes without Spack or the network."""

import io
import json
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

from jarvis_forge import apt_cli, spack_backend
from jarvis_forge.apt_progress import AptProgress
from jarvis_forge.cli import main
from jarvis_forge.package_operations import operation, run_command
from jarvis_forge.storage import read_json


RAW = "==> Concretized dependency tree: fixture@1.0 +shared ^tool@2.0\n"
DIAGNOSTIC = "Error: incompatible dependency at " + "/very-long-package-location" * 12


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    home = tmp_path / "forge"
    monkeypatch.setattr(apt_cli, "ensure_active", lambda home, progress=None: home)
    operations = []

    def transact(home, action, specs=(), *, dry_run=False, progress=None, **kwargs):
        if dry_run:
            return {"roots": list(specs), "dry_run": True}
        home.initialize()
        script = home.root / "environment.sh"
        script.write_text("true\n")
        with operation(home, "fixture", {}) as (op, result):
            operations.append(op)
            progress("[1/4] Resolve dependencies")
            progress("Spack concretize: concretize")
            error = (home.root / "fail").is_file()
            program = (f"import sys; sys.stdout.write({RAW!r}); "
                       f"sys.stderr.buffer.write(b'raw diagnostic\\xff\\n'); "
                       + (f"sys.stderr.write('==> ' + {DIAGNOSTIC!r} + '\\n    Keep this complete explanation.\\n'); sys.exit(7)"
                          if error else "sys.stdout.write('[+] abcdefg fixture@1.0 /prefix (0s)\\n')"))
            run_command([sys.executable, "-c", program], home.root, script, op,
                        "concretize", 10, progress=progress)
            progress("[2/4] Check installation plan: 1 package")
            progress("[3/4] Install packages")
            progress("[4/4] Save environment")
            result.update(roots=list(specs), packages=[{
                "name": "fixture", "version": "1.0", "root": True, "installed": True,
            }])
        return result

    monkeypatch.setattr(spack_backend, "transact", transact)
    return home, operations


def test_default_cli_is_short_and_archives_original_bytes(fake_install, capfdbinary):
    home, operations = fake_install
    assert main(["--home", str(home), "apt", "install", "fixture"]) == 0
    output = capfdbinary.readouterr()
    assert output.err.decode().splitlines() == [
        "[1/4] Resolve dependencies", "[2/4] Check installation plan: 1 package",
        "[3/4] Install packages", "[4/4] Save environment",
    ]
    assert b"APT install: passed" in output.out
    assert b"Concretized" not in output.err and b"Jarvis-Forge.Library" not in output.err
    assert b"\x1b[" not in output.err + output.out
    op = operations[0]
    assert (op / "command-concretize.stdout.log").read_bytes().startswith(RAW.encode())
    assert (op / "command-concretize.stderr.log").read_bytes() == b"raw diagnostic\xff\n"
    assert read_json(op / "command-concretize.json")["status"] == "passed"


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("flag", ["--verbose", "--debug"])
def test_detailed_mode_streams_raw_bytes_on_stderr(fake_install, capfdbinary, flag, position):
    home, _ = fake_install
    args = ["--home", str(home), "apt", "install", "fixture"]
    args = [flag, *args] if position == "before" else [*args, flag]
    assert main(args) == 0
    output = capfdbinary.readouterr()
    assert RAW.encode() in output.err and b"raw diagnostic\xff\n" in output.err
    assert b"Spack concretize: concretize" in output.err
    assert RAW.encode() not in output.out


@pytest.mark.parametrize("verbose", [False, True])
def test_json_is_one_object_with_no_ansi_or_raw_stdout(fake_install, capfdbinary, verbose):
    home, _ = fake_install
    assert main(["apt", "install", "fixture", "--home", str(home), "--json",
                 *(["--verbose"] if verbose else [])]) == 0
    output = capfdbinary.readouterr()
    assert json.loads(output.out)["ok"]
    assert (RAW.encode() in output.err) == verbose
    assert b"\x1b[" not in output.out + output.err


@pytest.mark.parametrize("verbose", [False, True])
def test_errors_are_complete_and_printed_once(fake_install, capfdbinary, monkeypatch, verbose):
    home, operations = fake_install
    home.mkdir()
    (home / "fail").touch()
    monkeypatch.setenv("COLUMNS", "28")
    assert main(["--home", str(home), "apt", "install", "fixture",
                 *(["--verbose"] if verbose else [])]) == 1
    output = capfdbinary.readouterr()
    assert output.out == b""
    assert output.err.count(DIAGNOSTIC.encode()) == 1
    assert output.err.count(b"Keep this complete explanation.") == 1
    assert b"..." not in output.err and b"Details:" not in output.err
    assert output.err.count(str(operations[0]).encode()) == 1
    assert b"Traceback" not in output.err


def test_terminal_bar_uses_reported_package_completions_and_stops(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    console = Console(file=io.StringIO(), force_terminal=True, width=90)
    progress = AptProgress(console)
    try:
        progress("[1/4] Resolve dependencies")
        progress("[3/4] Install packages")
        progress.installation_plan([
            {"hash": "abcdefg123", "name": "fixture", "installed": False, "external": False},
            {"hash": "qwertyu456", "name": "other", "installed": False, "external": False},
            {"hash": "old", "name": "existing", "installed": True, "external": False},
        ])
        task = progress.live.tasks[progress.packages_task]
        assert task.total == 2 and task.completed == 0
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 staging (0s)\r[ ] qwertyu other@2.0 build (2s)\n")
        assert task.description == "Packages · 2 in flight"
        first, second = progress.live.tasks[-2:]
        assert (first.description, second.description) == ("  fixture@1.0 · staging", "  other@2.0 · build")
        assert first.total is None and second.total is None
        progress.live.refresh()
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 configure (3s)\n")
        assert progress.live.tasks[-2:] == [first, second]
        assert first.description == "  fixture@1.0 · configure"
        assert task.completed == 0
        # Pipe chunks may split Unicode, ANSI sequences and status lines.
        for chunk in [b"[+] ignored other@1.0 /prefix\n", b"\x1b[32m[+] abc",
                      b"defg fixture@1.0 /prefix\x1b[0m", b"\n"]:
            progress.output("stdout", chunk)
        assert task.completed == 1 and task.description == "Packages · 1 in flight"
        assert first not in progress.live.tasks and second in progress.live.tasks
        progress.output("stderr", b"[+] abcdefg fixture@1.0 /prefix\n")
        progress.output("stdout", b"[x] qwertyu other@2.0 failed: /log (3s)\n")
        assert task.completed == 1 and task.description == "Packages · 0 in flight"
        assert second not in progress.live.tasks
    finally:
        progress.close()
    assert not progress.live.live.is_started


def test_in_flight_rows_clear_on_cache_miss_command_end_and_replan(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    progress = AptProgress(Console(file=io.StringIO(), force_terminal=True, width=90))
    fixture = {"hash": "abcdefg123", "name": "fixture", "installed": False, "external": False}
    try:
        progress("[3/4] Install packages")
        progress.installation_plan([fixture])
        task = progress.live.tasks[progress.packages_task]
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 fetching from build cache (0s)\n")
        assert task.description == "Packages · 1 in flight"
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 no binary available (2s)\n")
        assert task.description == "Packages · 0 in flight" and task.completed == 0
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 staging (0s)\n")
        progress.output("stdout", b"[+] abcdefg fixture@1.0 /prefix (2s)")
        progress.output_finished()
        assert task.description == "Packages · 0 in flight" and task.completed == 1

        progress.installation_plan([fixture])
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 build (0s)\n")
        progress.output_finished()
        assert task.description == "Packages · 0 in flight" and task.completed == 0

        progress.output("stdout", b"[ ] abcdefg fixture@1.0 staging (0s)\n")
        progress.installation_plan([])
        assert not task.visible and not progress.in_flight
    finally:
        progress.close()


@pytest.mark.parametrize("kwargs", [{"as_json": True}, {"verbose": True}, {}])
def test_plain_modes_disable_live_animation(monkeypatch, kwargs):
    monkeypatch.setenv("NO_COLOR", "1")
    console = Console(file=io.StringIO(), force_terminal=True)
    progress = AptProgress(console, **kwargs)
    assert progress.live is None
    progress("[1/4] Resolve dependencies")
    progress.close()
    assert "\x1b[" not in console.file.getvalue()


def test_check_progress_has_two_phases():
    console = Console(file=io.StringIO(), force_terminal=False)
    progress = AptProgress(console, check=True)
    progress("[1/4] Resolve dependencies")
    progress("[2/4] Check installation plan")
    progress.close(success=True)
    assert console.file.getvalue().splitlines() == [
        "[1/2] Resolve dependencies", "[2/2] Check installation plan"]


@pytest.mark.parametrize("callback,backend,kwargs", [
    ("update_sources", "update", {}),
    ("spack_compilers", "compilers", {"find": True}),
    ("import_spack", "switch", {"source": "spack"}),
    ("switch_source", "switch", {"source": "spack", "name": "default",
                                  "direct": False, "archived": False}),
    ("autoremove", "autoremove", {"yes": True, "dry_run": False}),
    ("sources", "sources", {"add": "fixture", "url": "https://example.org/cache",
                             "remove": None, "trust_key": None, "yes": False}),
])
def test_callbacks_use_the_supplied_context_without_a_click_stack(
    tmp_path, monkeypatch, capsys, callback, backend, kwargs
):
    from jarvis_forge.storage import ForgeHome

    # Typer may use its own Click implementation, whose context stack is
    # separate from the installed click package. Callbacks already receive ctx.
    home = ForgeHome(tmp_path / "absent")
    progress = AptProgress(Console(file=io.StringIO(), force_terminal=False))
    ctx = SimpleNamespace(obj=SimpleNamespace(home=home, user_home=home,
                          apt_progress=progress, as_json=True))
    calls = []

    def ready(home, *, progress):
        assert progress is ctx.obj.apt_progress
        return home

    def run(*args, **options):
        assert options["progress"] is ctx.obj.apt_progress
        calls.append(options)
        return {"status": "passed"}

    monkeypatch.setattr(apt_cli, "ensure_active", ready)
    monkeypatch.setattr(apt_cli if backend == "switch" else spack_backend, backend, run)
    getattr(apt_cli, callback)(ctx, **kwargs)
    assert json.loads(capsys.readouterr().out)["ok"]
    assert len(calls) == 1 and progress.closed
    assert not home.root.exists()


def test_update_command_passes_one_progress_instance_through_preparation(
    tmp_path, monkeypatch, capsys
):
    seen = []

    def ready(home, *, progress):
        seen.append(progress)
        return home

    def update(home, *, progress):
        assert progress is seen[0]
        progress("Updated package catalog")
        return {"status": "passed", "available": 12}

    monkeypatch.setattr(apt_cli, "ensure_active", ready)
    monkeypatch.setattr(spack_backend, "update", update)
    assert main(["--home", str(tmp_path / "absent"), "apt", "update", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["available"] == 12
    assert "Updated package catalog" in output.err
    assert seen[0].closed
