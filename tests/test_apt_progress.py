"""Exercise the CLI and real subprocess pipes without Spack or the network."""

import io
import json
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

from jarvis_forge import apt_cli, build_catalog, spack_backend
from jarvis_forge.apt_progress import AptProgress
from jarvis_forge.cli import main
from jarvis_forge.package_operations import operation, run_command
from jarvis_forge.storage import ForgeError, read_json

pytestmark = pytest.mark.usefixtures("linux_apt")


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
        assert task.description == "Packages · 2 running · 0 queued"
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
        assert task.completed == 1 and task.description == "Packages · 1 running · 0 queued"
        assert first not in progress.live.tasks and second in progress.live.tasks
        progress.output("stderr", b"[+] abcdefg fixture@1.0 /prefix\n")
        progress.output("stdout", b"[x] qwertyu other@2.0 failed: /log (3s)\n")
        assert task.completed == 1 and task.description == "Packages · 0 running · 0 queued · 1 failed"
        assert second not in progress.live.tasks
    finally:
        progress.close()
    assert not progress.live.live.is_started


def test_status_events_survive_concurrent_output_on_the_same_line(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    progress = AptProgress(Console(file=io.StringIO(), force_terminal=True, width=90))
    try:
        progress("[3/4] Install packages")
        progress.installation_plan([
            {"hash": "abcdefg123", "name": "fixture", "installed": False, "external": False},
            {"hash": "qwertyu456", "name": "other", "installed": False, "external": False},
        ])
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 build (2s)\n")
        task = progress.live.tasks[progress.packages_task]
        assert task.description == "Packages · 1 running · 1 queued"
        # A configure prompt has no newline; another package finishes mid-line.
        progress.output("stdout", b"checking if install accepts -p option... \x1b[32m[+] abc")
        progress.output("stdout", b"defg fixture@1.0 /prefix (36s)\x1b[0m[ ] qwertyu other@2.0 build (1s)\n")
        assert task.completed == 1 and task.description == "Packages · 1 running · 0 queued"
        assert "abcdefg123" not in progress.in_flight
        assert progress.live.tasks[-1].description == "  other@2.0 · build"
        # Interleaved unknown status-like output must not change the plan.
        progress.output("stdout", b"checking header... [+] unknown unrelated@1.0 /elsewhere\n")
        assert task.completed == 1
        progress.output("stdout", b"checking for malloc.h... [+] qwertyu other@2.0 /prefix (25s)\n")
        assert task.completed == 2 and task.description == "Packages · 0 running · 0 queued"
        assert not progress.in_flight
    finally:
        progress.close()


@pytest.mark.parametrize("width", [40, 60, 80, 120])
def test_aligned_package_rows_show_phase_and_total_time_separately(monkeypatch, width):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    progress = AptProgress(Console(file=io.StringIO(), force_terminal=True, width=width))
    clock = [100.0]
    progress.live.get_time = lambda: clock[0]
    try:
        progress("[1/4] Resolve dependencies")
        progress("[3/4] Install packages")
        progress.installation_plan([
            {"hash": digest, "name": name, "installed": False, "external": False}
            for digest, name in (("abcdefg123", "fixture"), ("qwertyu456", "other"), ("pending123", "queued"))
        ])
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 configure (0s)\n")
        package = progress.live.tasks[-1]
        clock[0] = 175.0
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 configure (75s)\n")
        assert package.fields["phase_started"] == 100.0
        clock[0] = 220.0
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 build (2m0s)checking for header...\n")
        assert package.fields["phase"] == "build" and package.fields["phase_started"] == 220.0
        clock[0] = 250.0
        progress.output("stdout", b"[+] qwertyu other@2.0 /prefix (1s)\n")
        task = progress.live.tasks[progress.packages_task]
        assert task.description == "Packages · 1 running · 1 queued"
        screen = io.StringIO()
        Console(file=screen, width=width, color_system=None).print(progress.live.get_renderable())
        display = screen.getvalue()
        assert "fixture@1.0" in display and "build" in display
        assert "00:30" in display and "02:30" in display
        assert "other@2.0" not in display
        if width >= 80:
            assert "Step 3/4" in display and "1/3" in display and "33%" in display
            assert "Phase time" in display and "Total time" in display and "Activity" in display
        assert "..." not in display and "\x1b[" not in display
    finally:
        progress.close()


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
        assert task.description == "Packages · 1 running · 0 queued"
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 no binary available (2s)\n")
        assert task.description == "Packages · 0 running · 1 queued" and task.completed == 0
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 staging (0s)\n")
        progress.output("stdout", b"[+] abcdefg fixture@1.0 /prefix (2s)")
        progress.output_finished()
        assert task.description == "Packages · 0 running · 0 queued" and task.completed == 1

        progress.installation_plan([fixture])
        progress.output("stdout", b"[ ] abcdefg fixture@1.0 build (0s)\n")
        progress.output_finished()
        assert task.description == "Packages · 0 running · 1 queued" and task.completed == 0

        progress.output("stdout", b"[ ] abcdefg fixture@1.0 staging (0s)\n")
        progress.installation_plan([])
        assert not task.visible and not progress.in_flight
    finally:
        progress.close()


def test_validated_success_clears_queued_work_even_if_a_display_event_was_missing(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    progress = AptProgress(Console(file=io.StringIO(), force_terminal=True, width=90))
    progress("[3/4] Install packages")
    progress.installation_plan([
        {"hash": "abcdefg123", "name": "fixture", "installed": False, "external": False},
        {"hash": "qwertyu456", "name": "other", "installed": False, "external": False},
    ])
    progress.output("stdout", b"[ ] abcdefg fixture@1.0 build (2s)\n")
    progress.close(success=True)
    task = progress.live.tasks[progress.packages_task]
    assert task.completed == task.total == 2
    assert task.description == "Packages · 0 running · 0 queued"
    assert not progress.in_flight


def test_download_activity_cleans_up_without_stopping_active_spack_progress(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    screen = io.StringIO()
    progress = AptProgress(Console(file=screen, force_terminal=True, width=110))
    try:
        progress("[1/4] Resolve dependencies")
        main_task, = progress.live.tasks
        with pytest.raises(ForgeError, match="fixture failed"), progress.activity("Fetch source", download=True) as update:
            update(1024, None)
            assert "1.0/? KiB" in screen.getvalue()
            update(2048, 4096)
            assert "2.0/4.0 KiB" in screen.getvalue()
            raise ForgeError("fixture failed")
        assert progress.live.tasks == [main_task]
        assert progress.live.live.is_started
    finally:
        progress.close()
    assert not progress.live.live.is_started


@pytest.mark.parametrize("kwargs", [{"as_json": True}, {"verbose": True}, {}])
def test_plain_modes_disable_live_animation(monkeypatch, kwargs):
    monkeypatch.setenv("NO_COLOR", "1")
    console = Console(file=io.StringIO(), force_terminal=True)
    progress = AptProgress(console, **kwargs)
    assert progress.live is None
    progress("[1/4] Resolve dependencies")
    with progress.activity("Fetch source", download=True) as update:
        assert update is None
    progress.close()
    assert "Fetch source\n" in console.file.getvalue()
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
    if callback == "update_sources":
        monkeypatch.setattr(apt_cli, "initialize_profile", lambda home: None)
        monkeypatch.setattr(build_catalog, "update", lambda home: {
            "platform": "linux", "builds": [], "excluded_builds": {},
        })
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
    monkeypatch.setattr(apt_cli, "initialize_profile", lambda home: None)
    monkeypatch.setattr(spack_backend, "update", update)
    catalog = {"platform": "linux", "builds": [], "excluded_builds": {}}
    monkeypatch.setattr(build_catalog, "update", lambda home: catalog)
    assert main(["--home", str(tmp_path / "absent"), "apt", "update", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["available"] == 12
    assert json.loads(output.out)["build_catalog"] == catalog
    assert "Updated package catalog" in output.err
    assert seen[0].closed


def test_apt_update_reports_partial_success_in_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(apt_cli, "initialize_profile", lambda home: None)
    monkeypatch.setattr(apt_cli, "ensure_active", lambda home, **kwargs: home)
    monkeypatch.setattr(spack_backend, "update", lambda home, **kwargs: {"status": "passed", "path": "/packages"})

    def fail(home):
        with operation(home, "build-catalog-update", {}) as (directory, _):
            (directory / "index.stderr.log").write_text("network error\n")
            raise ForgeError("Failed to download build index", code=1)

    from jarvis_forge.storage import ForgeHome
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    monkeypatch.setattr(build_catalog, "update", fail)
    assert main(["--home", str(home.root), "apt", "update", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "apt update" and not result["ok"]
    assert result["package_update"]["status"] == "passed"
    assert result["build_catalog"]["status"] == "failed"
    assert (home.root / result["build_catalog"]["path"] / "index.stderr.log").is_file()


def test_completed_package_rows_remain_in_scrollback_when_next_package_starts(monkeypatch):
    from rich.console import Group
    from rich.table import Table
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    clock = [10.0]
    screen = io.StringIO()
    console = Console(file=screen, force_terminal=True, width=110, record=True)
    progress = AptProgress(console)
    progress.live.get_time = lambda: clock[0]
    printed = []
    original_print = console.print

    def retain(*objects, **kwargs):
        if objects and isinstance(objects[0], (Table, Group)):
            capture = io.StringIO()
            Console(file=capture, width=110).print(objects[0])
            text = capture.getvalue().rstrip()
            if text.startswith('✓ '):
                printed.append(text)
        return original_print(*objects, **kwargs)

    monkeypatch.setattr(console, 'print', retain)
    try:
        progress('[3/4] Install packages')
        progress.installation_plan([
            {'hash': 'abcdefg111', 'name': 'first', 'version': '1.0', 'external': False, 'installed': False},
            {'hash': 'hijklmn222', 'name': 'next', 'version': '2.0', 'external': False, 'installed': False},
        ])
        progress.output('stdout', b'[ ] abcdefg first@1.0 build (0s)\n')
        clock[0] = 75.0
        progress.output('stdout', b'[+] abcdefg first@1.0 /prefix (65s)\n')
        progress.output('stderr', b'[+] abcdefg first@1.0 /prefix (65s)\n')
        progress.output('stdout', b'[ ] hijklmn next@2.0 build (0s)\n')
        assert len(printed) == 1
        assert printed[0].split() == ['✓', 'first@1.0', 'installed', 'build', '01:05', 'total', '01:05']
        assert len(progress.completed_rows) == 1
        assert progress.completed_rows[0]['elapsed_seconds'] == 65
        assert set(progress.in_flight) == {'hijklmn222'}
        clock[0] = 100.0
        progress.output('stdout', b'[+] hijklmn next@2.0 /prefix (25s)\n')
        progress.close(success=True)
        assert len(printed) == 2
        assert printed[1].split() == ['✓', 'next@2.0', 'installed', 'build', '00:25', 'total', '00:25']
        for column in ('installed', 'build', 'total'):
            assert printed[0].index(column) == printed[1].index(column)
        retained = console.export_text()
        assert all(row in retained for row in printed)
        assert [row['elapsed_seconds'] for row in progress.completed_rows] == [65, 25]
    finally:
        progress.close()
