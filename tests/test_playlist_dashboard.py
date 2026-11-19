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
