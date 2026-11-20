import sys
import time

import pytest
import yaml

from jarvis_forge import playlist_collections as collections, playlist_jobs as jobs, playlist_library as library
from jarvis_forge.dashboard.server import Dashboard
from jarvis_forge.storage import ForgeError, ForgeHome, read_json


@pytest.fixture
def sample(tmp_path):
    home = ForgeHome(tmp_path / "home")
    home.initialize()
    directory = tmp_path / "library"
    project = tmp_path / "sample"
    directory.mkdir()
    project.mkdir()
    (project / "tests.yaml").write_text("""schema_version: 2
kind: checker
id: local
profiles:
  daily: [echo]
  acceptance: [echo]
default_profile: daily
checks:
  - {id: preflight, argv: [echo, prepared], cache: false}
  - {id: echo, argv: [echo, ready], needs: [preflight], cache: false}
""")
    (project / "run_demo.py").write_text(
        """import json, pathlib, sys
if sys.argv[1] == "list":
    print(json.dumps({"cases": [{"id": "math::1,2", "title": "Arithmetic"}]}))
else:
    request = json.loads(pathlib.Path(sys.argv[2]).read_text())
    work = pathlib.Path(sys.argv[3])
    print("example output", flush=True)
    (work / "result.json").write_text(json.dumps(
        {"cases": [{"id": case, "status": "passed", "seconds": 0.1} for case in request["cases"]]}))
""")
    runner = [sys.executable, "run_demo.py"]
    (directory / "library.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "kind": "playlist-library", "id": "example", "title": "Example",
        "shelves": [{"id": "main", "title": "Main"}],
        "projects": [{"id": "demo", "title": "Demo", "shelf": "main", "root": "../sample", "sources": [
            {"id": "checks", "kind": "test", "path": "tests.yaml"},
            {"id": "calc", "kind": "command", "list": [*runner, "list"],
             "run": [*runner, "run", "${request}", "${result_dir}"]}]}],
    }))
    return home, directory, library.load(directory)


def test_derived_and_saved_playlists(sample):
    home, directory, selected = sample
    rows, diagnostics = collections.catalog(home, selected)
    assert not diagnostics
    profiles = {row["id"]: row["members"] for row in rows}
    assert profiles["demo/checks@daily"] == profiles["library@daily"] == profiles["main@daily"]
    assert profiles["library@acceptance"] == ["demo/checks/preflight", "demo/checks/echo"]
    item = collections.create(home, selected, "smoke", title="Smoke",
                              entries=[{"profile": "demo/checks@daily"}, {"case": "demo/checks/preflight"}])
    assert item["members"] == profiles["library@daily"]
    path = directory / "playlists/smoke.json"
    saved = path.read_bytes()
    listed = collections.show(home, selected, "smoke")
    assert listed["digest"] == item["digest"] and path.read_bytes() == saved
    with pytest.raises(ForgeError, match="already contains"):
        collections.edit(home, selected, "smoke", "add", [{"case": "demo/checks/preflight"}])
    with pytest.raises(ForgeError, match="since it was opened"):
        collections.edit(home, selected, "smoke", "remove", item["entries"], expected="stale")
    assert path.read_bytes() == saved


def test_frozen_edit_has_reason_and_history(sample):
    home, directory, selected = sample
    item = collections.create(home, selected, "selection", title="Selection",
                              entries=[{"case": "demo/calc/math::1,2"}])
    path = directory / "playlists/selection.json"
    spec = read_json(path)
    library._write(path, {**spec, "frozen": {"job": "older"}})
    with pytest.raises(ForgeError, match="Reason"):
        collections.edit(home, selected, "selection", "remove", item["entries"])
    result = collections.edit(home, selected, "selection", "remove", item["entries"], reason="Replace demo")
    assert result["changes"][0]["reason"] == "Replace demo"
    assert not result["members"]


def test_dashboard_dispatches_and_reads_real_jobs(sample):
    home, directory, selected = sample
    app = Dashboard(home, directory, "local-token")
    inventory = app.dispatch("GET", "/api/catalog", {}, {})
    assert len(inventory["cases"]) == 3
    result = app.dispatch("POST", "/api/playlists", {}, {
        "id": "demo-run", "title": "Demo run",
        "entries": [{"case": "demo/calc/math::1,2"}, {"case": "demo/checks/echo"}],
    })
    playlist = result["playlist"]
    queued = app.dispatch("POST", "/api/jobs", {}, {"playlist": playlist["id"]})["job"]
    assert queued["status"] == "queued"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = app.dispatch("GET", f"/api/jobs/{queued['id']}", {}, {})["job"]
        if result["status"] not in jobs.ACTIVE:
            break
        time.sleep(0.1)
    assert result["status"] == "passed"
    log = app.dispatch("GET", f"/api/jobs/{queued['id']}/log", {"case": ["demo/calc/math::1,2"]}, {})
    assert "example output" in log["text"]
    assert {row["status"] for row in result["cases"]} == {"passed"}
    with pytest.raises(ForgeError, match="not part"):
        app.dispatch("GET", f"/api/jobs/{queued['id']}/log", {"case": ["../anything"]}, {})


def test_http_boundary(sample):
    from io import BytesIO
    from types import SimpleNamespace
    from email.message import Message
    from jarvis_forge.dashboard.server import Handler

    home, directory, _ = sample
    app = Dashboard(home, directory, "local-token")

    def call(method="GET", path="/api/catalog", token=True, **headers):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8787, dashboard=app)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = headers.pop("Host", "127.0.0.1:8787")
        if token:
            handler.headers["X-Jforge-Token"] = app.token
        for key, value in headers.items():
            handler.headers[key] = value
        handler.rfile = BytesIO(b"{}")
        handler.wfile = BytesIO()
        events = []
        handler._write = lambda status, data, *rest: events.append((status, data))
        handler._headers = lambda status, **kw: events.append((status, kw))
        handler._request(method)
        return events[0]

    assert call(token=False)[0] == 401
    assert call(Host="other.test:8787")[0] == 403
    assert call("POST", "/api/sync", Origin="https://other.test")[0] == 403
    assert call("POST", "/api/sync", **{"Content-Type": "text/plain"})[0] == 415
    assert call("POST", "/api/sync", **{"Content-Length": "1048577", "Content-Type": "application/json"})[0] == 413
    status, data = call(path="/?token=local-token", token=False)
    assert status == 303 and ("Location", "/") in data["extra"]
    cookie = next(value for key, value in data["extra"] if key == "Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert call()[0] == 200
    assert call("GET", "/components.js")[0] == 200
    assert call("GET", "/api.js")[0] == 200
    assert call("GET", "/preview-api.js")[0] == 200
    assert call("GET", "/demo-data.js")[0] == 200
    from jarvis_forge.storage import write_json
    work = jobs._base(home, app.selected()) / "job-http"
    write_json(work / "state.json", {"id": "job-http", "cases": [
        {"id": "demo/calc/math::1,2", "unit": 0, "kind": "command"}]})
    (work / "unit-0").mkdir()
    (work / "unit-0/index.html").write_text("<h1>Actual output</h1>")
    assert call(path="/api/jobs/job-http/artifacts", token=False)[0] == 401
    capability = app.artifact_token("job-http", "0")
    status, headers = call(path=f"/api/jobs/job-http/artifacts/0/{capability}/index.html", token=False)
    assert status == 200 and headers["artifact"] is True and headers["content_type"] == "text/html"
    assert call(path=f"/api/jobs/job-http/artifacts/0/{capability}/../state.json")[0] == 404
    assert call(path=f"/api/jobs/job-http/artifacts/1/{capability}/index.html", token=False)[0] == 401
    assert call(path=f"/api/jobs/job-other/artifacts/0/{capability}/index.html", token=False)[0] == 401
    assert call(path="/api/jobs/job-http/artifacts/0/wrong/index.html")[0] == 403


def test_profile_job_retains_acceptance_and_streams_steps(sample):
    home, directory, selected = sample
    project = directory.parent / "sample"
    text = (project / "tests.yaml").read_text()
    text = text.replace('argv: [echo, ready]',
                        'argv: [' + sys.executable + ', -c, "import time; print(123, flush=True); time.sleep(1)"]')
    (project / "tests.yaml").write_text(text)
    item = collections.create(home, selected, "acceptance", title="Full acceptance",
                              entries=[{"profile": "demo/checks@acceptance"}])
    first = jobs.submit(home, selected, name=item["id"])
    with pytest.raises(ForgeError, match="already running or queued"):
        jobs.submit(home, selected, name=item["id"])
    observed = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = jobs.get(home, selected, first["id"])
        observed.append(state)
        if state["status"] not in jobs.ACTIVE:
            break
        time.sleep(0.05)
    assert state["status"] == "passed", state
    assert any(row["status"] == "running" and row["cases"][0]["status"] == "passed"
               and row["cases"][1]["status"] == "running" for row in observed)
    run = read_json(home.run_path(state["cases"][0]["run_id"]) / "request.json")
    assert run["profile"] == "acceptance"
    assert state["acceptance"]["status"] == "not accepted"
    with pytest.raises(ForgeError, match="has started"):
        jobs.cancel(home, selected, first["id"], only_if_queued=True)


def test_artifacts_are_confined_and_reused_outputs_resolve(sample):
    from jarvis_forge.storage import write_json
    home, _, selected = sample
    directory = jobs._base(home, selected) / "job-output"
    write_json(directory / "state.json", {"id": "job-output", "cases": [
        {"id": "demo/checks/echo", "unit": 0, "kind": "check", "run_id": "checker-current",
         "reused_from": "checker-origin"}]})
    for name in ("checker-current", "checker-origin"):
        write_json(home.root / "runs" / name / "request.json", {})
    root = home.root / "runs/checker-origin/steps/echo"
    root.mkdir(parents=True)
    (root / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    (root / "index.html").write_text('<img src="figure.png">')
    (root / "escape.png").symlink_to(home.root / "config.yaml")
    rows = jobs.artifacts(home, selected, "job-output")
    assert {row["name"] for row in rows} == {"figure.png", "index.html"}
    assert jobs.artifact_path(home, selected, "job-output", "0", "figure.png").read_bytes() == b"\x89PNG\r\n\x1a\ncontent"
    for index, relative in [("0", "../request.json"), ("0", "/etc/passwd"), ("0", "escape.png"), ("9", "figure.png")]:
        with pytest.raises(ForgeError):
            jobs.artifact_path(home, selected, "job-output", index, relative)


def test_album_queue_executes_in_submission_order(sample):
    home, _, selected = sample
    first = jobs.submit(home, selected, cases=["demo/checks/echo"])
    second = jobs.submit(home, selected, cases=["demo/calc/math::1,2"])
    first = jobs.wait_job(home, selected, first["id"])
    second = jobs.wait_job(home, selected, second["id"])
    assert first["status"] == second["status"] == "passed"
    assert first["finished_at"] <= second["started_at"]


def test_failed_check_retains_real_failure_and_output(sample):
    home, directory, selected = sample
    project = directory.parent / "sample"
    spec = yaml.safe_load((project / "tests.yaml").read_text())
    spec["checks"][1]["argv"] = [sys.executable, "-c", "print('real failure output'); raise SystemExit(1)"]
    (project / "tests.yaml").write_text(yaml.safe_dump(spec))
    state = jobs.submit(home, selected, cases=["demo/checks/echo"])
    result = jobs.wait_job(home, selected, state["id"])
    assert result["status"] == "failed"
    assert result["cases"][-1]["status"] == "failed"
    assert "real failure output" in jobs.tail(home, selected, state["id"], "demo/checks/echo")


def test_dashboard_config_read_does_not_initialize_home(tmp_path, capsys):
    from jarvis_forge.cli import main
    import json
    home = tmp_path / "uninitialized"
    assert main(["--home", str(home), "dashboard", "config", "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["port"] == 8787 and value["library_path"] is None
    assert not home.exists()


def test_dashboard_config_cli_persists_absolute_paths_and_controls_service(sample, monkeypatch, capsys):
    from jarvis_forge.cli import main
    from jarvis_forge.dashboard import config, server
    import json
    home, directory, _ = sample
    monkeypatch.chdir(directory.parent)
    args = ["--home", str(home.root), "dashboard"]
    assert main([*args, "config", "--library", "library", "--port", "9876", "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["command"] == "dashboard config" and saved["library_path"] == str(directory)
    assert read_json(config.path(home)) == {"schema_version": 1, "library_path": str(directory), "port": 9876}
    monkeypatch.chdir(home.root)
    calls = []
    record = {"pid": 123, "port": 9876, "library_path": str(directory), "url": "http://127.0.0.1:9876/?token=test"}
    def start(selected_home, selected_library, port, **options):
        calls.append(("start", selected_home.root, selected_library, port))
        return record
    def stop(selected_home):
        calls.append(("stop", selected_home.root))
        return {"stopped": True}
    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "stop", stop)
    monkeypatch.setattr(server, "status", lambda selected_home: record)
    for operation in ("start", "status", "restart", "stop"):
        assert main([*args, operation, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["command"] == f"dashboard {operation}"
    assert calls == [("start", home.root, directory, 9876), ("stop", home.root),
                     ("start", home.root, directory, 9876), ("stop", home.root)]
    assert main([*args, "start", "--port", "0", "--json"]) == 0
    capsys.readouterr()
    assert calls[-1][-1] == 0 and config.load(home)["port"] == 9876


def test_dashboard_settings_fall_back_to_playlist_selection(sample):
    from jarvis_forge.dashboard import config
    home, directory, _ = sample
    library.use(home, directory)
    assert config.resolve(home) == (directory, 8787)
    assert not config.path(home).exists()


@pytest.mark.parametrize("bad", [{"schema_version": True, "library_path": None, "port": 8787},
                                 {"schema_version": 1, "library_path": "relative", "port": 8787},
                                 {"schema_version": 1, "library_path": None, "port": True},
                                 {"schema_version": 1, "library_path": None, "port": 65536}])
def test_dashboard_rejects_bad_saved_configuration(sample, bad):
    from jarvis_forge.dashboard import config
    from jarvis_forge.storage import write_json
    home, _, _ = sample
    write_json(config.path(home), bad)
    with pytest.raises(ForgeError):
        config.load(home)


def test_dashboard_restart_checks_library_before_stopping(sample, monkeypatch, capsys):
    from jarvis_forge.cli import main
    from jarvis_forge.dashboard import config, server
    from jarvis_forge.storage import write_json
    import json
    home, directory, _ = sample
    write_json(config.path(home), {"schema_version": 1, "library_path": str(directory / "missing"), "port": 8787})
    monkeypatch.setattr(server, "stop", lambda home: pytest.fail("A valid service must not be stopped"))
    assert main(["--home", str(home.root), "dashboard", "restart", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("operation", ["config", "start", "status", "stop", "restart"])
def test_dashboard_cli_help(operation, capsys):
    from jarvis_forge.cli import main
    assert main(["dashboard", operation, "-h"]) == 0
    output = capsys.readouterr().out
    assert f"Usage: jforge dashboard {operation}" in output
    assert "Show this message and exit." in output and "\x1b[" not in output
