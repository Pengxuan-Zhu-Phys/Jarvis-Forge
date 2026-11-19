"""Authenticated, loopback-only interface to a local playlist library."""

from __future__ import annotations

from collections import defaultdict
from http import HTTPStatus
from http.client import HTTPConnection
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import subprocess
import sys
from threading import Lock
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .. import playlist_collections as collections, playlist_jobs as jobs, playlist_library as library
from ..builds import file_lock
from ..storage import ForgeError, ForgeHome, read_json, write_json


STATIC = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
          "/app.js": ("app.js", "text/javascript"), "/components.js": ("components.js", "text/javascript")}
COOKIE = "jforge_dashboard"


class Dashboard:
    def __init__(self, home, directory, token):
        self.home = home
        self.directory = directory
        self.token = token
        self.lock = Lock()
        self.inventory = None
        self.expires = 0

    def selected(self):
        return library.load(self.directory)

    def invalidate(self):
        self.expires = 0

    def catalog(self, selected):
        if self.expires <= time.monotonic():
            self.inventory = library.inventory(self.home, selected)
            self.expires = time.monotonic() + 3
        inventory = self.inventory
        playlists, errors = collections.catalog(self.home, selected, inventory=inventory)
        recent = jobs.list_jobs(self.home, selected, 100)
        values = defaultdict(dict)
        for job in reversed(recent):
            for row in job["cases"]:
                values[row["id"]] = {"status": row["status"], "seconds": row.get("seconds"),
                                     "job": job["id"]}
        return {"library": {key: selected[key] for key in ("id", "title")},
                "host": {"system": platform.system(), "machine": platform.machine()},
                "shelves": selected["shelves"], "projects": inventory["projects"],
                "cases": [{**row, "last": values.get(row["id"])} for row in inventory["cases"]],
                "playlists": playlists, "jobs": recent,
                "messages": inventory["errors"] + errors + inventory["warnings"]}

    def dispatch(self, method, path, query, body):
        if path == "/api/service" and method == "GET":
            return {"pid": os.getpid()}
        with self.lock:
            selected = self.selected()
            parts = path.split("/")
            if method == "GET":
                if path == "/api/catalog":
                    return self.catalog(selected)
                if path == "/api/jobs":
                    return {"jobs": jobs.list_jobs(self.home, selected, 100)}
                if parts[:3] == ["", "api", "jobs"] and len(parts) in (4, 5):
                    if len(parts) == 4:
                        return {"job": jobs.get(self.home, selected, parts[3])}
                    if parts[4] == "log":
                        case = query.get("case", [""])[0]
                        return {"text": jobs.tail(self.home, selected, parts[3], case)}
            elif method == "POST" and path == "/api/sync":
                self.invalidate()
                return self.catalog(selected)
            elif method == "POST" and path == "/api/jobs":
                library._fields(body, "Job", (), ("playlist", "cases", "jobs", "env", "fresh"))
                name = library._text(body["playlist"], "Playlist ID") if "playlist" in body else None
                cases = library._strings(body.get("cases", []), "Case IDs")
                job = jobs.submit(self.home, selected, name=name, cases=cases, jobs=body.get("jobs", 1),
                                  environment=body.get("env"), fresh=body.get("fresh", False))
                return {"job": job}
            elif method == "POST" and parts[:3] == ["", "api", "jobs"] and len(parts) == 5 and parts[4] == "cancel":
                return {"job": jobs.cancel(self.home, selected, parts[3])}
            elif path == "/api/playlists" and method == "POST":
                library._fields(body, "Playlist", ("id", "title", "entries"), ("goal", "shelf", "environment"))
                self._idle(selected)
                name = library._text(body.pop("id"), "Playlist ID")
                return {"playlist": collections.create(self.home, selected, name, **body)}
            elif parts[:3] == ["", "api", "playlists"] and len(parts) == 4 and method == "PUT":
                library._fields(body, "Playlist edit", ("op", "entries", "digest"), ("reason",))
                self._idle(selected)
                if not isinstance(body["entries"], list):
                    raise ForgeError("Playlist entries must be a list.")
                expected = library._text(body["digest"], "Playlist digest")
                return {"playlist": collections.edit(self.home, selected, parts[3], body["op"], body["entries"],
                                                     reason=body.get("reason"), expected=expected)}
        raise ForgeError("Unknown dashboard endpoint.", code=404)

    def _idle(self, selected):
        if any(job["status"] in jobs.ACTIVE for job in jobs.list_jobs(self.home, selected)):
            raise ForgeError("Wait until scheduled jobs finish before editing playlists.", code=409)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Initial URLs contain a session token.
        return

    def _headers(self, status, *, content_type=None, length=0, extra=()):
        self.send_response(status)
        for name, value in (("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                            ("X-Content-Type-Options", "nosniff"),
                            ("Content-Security-Policy", "default-src 'self'; object-src 'none'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                            ("Content-Length", str(length)), *extra):
            self.send_header(name, value)
        if content_type:
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.end_headers()

    def _write(self, status, payload, kind="application/json"):
        if kind == "application/json":
            payload = json.dumps(payload, allow_nan=False).encode("utf-8")
        self._headers(status, content_type=kind, length=len(payload))
        self.wfile.write(payload)

    def _error(self, exc):
        self._write(exc.code if 400 <= exc.code < 600 else 400,
                    {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc), "hint": exc.hint}})

    def _cookie(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        name = f"{COOKIE}_{self.server.server_port}"
        return cookie[name].value if name in cookie else ""

    def _authorized(self, value):
        return hmac.compare_digest(value.encode("utf-8"), self.server.dashboard.token.encode("ascii"))

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise ForgeError("Expected application/json.", code=415)
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if size < 0 or size > 1_048_576:
            raise ForgeError("JSON request must be at most 1 MB.", code=413)
        try:
            body = json.loads(self.rfile.read(size))
        except (UnicodeError, ValueError, OSError) as exc:
            raise ForgeError("Invalid JSON.") from exc
        if not isinstance(body, dict):
            raise ForgeError("Request body must be a JSON object.")
        return body

    def _request(self, method):
        try:
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                raise ForgeError("Invalid Host.", code=403)
            try:
                url = urlsplit(self.path)
            except ValueError as exc:
                raise ForgeError("Invalid dashboard URL.") from exc
            query = parse_qs(url.query)
            if method == "GET" and url.path == "/" and self._authorized(query.get("token", [""])[0]):
                self._headers(303, extra=(("Location", "/"), ("Set-Cookie",
                              f"{COOKIE}_{self.server.server_port}={self.server.dashboard.token}; Path=/; HttpOnly; SameSite=Strict")))
                return
            if not any(self._authorized(value) for value in (self._cookie(), self.headers.get("X-Jforge-Token", ""))):
                if url.path == "/":
                    content = (b"<!doctype html><html lang='en'><title>Forge Playlists</title>"
                               b"<h1>Open the dashboard link from jforge dashboard start.</h1></html>")
                    self._write(401, content, "text/html")
                    return
                raise ForgeError("Dashboard session required.", code=401)
            if method != "GET" and self.headers.get("Origin") not in {None, f"http://{host}"}:
                raise ForgeError("Invalid Origin.", code=403)
            if method == "GET" and url.path in STATIC:
                name, kind = STATIC[url.path]
                asset = files(__package__).joinpath("static", name).read_bytes()
                self._write(200, asset, kind)
                return
            data = self.server.dashboard.dispatch(method, unquote(url.path), query,
                                                  self._body() if method in {"POST", "PUT"} else {})
            self._write(200, {"ok": True, **data})
        except ForgeError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, TypeError, OSError) as exc:
            self._error(ForgeError(str(exc), code=400))

    def do_GET(self):
        self._request("GET")

    def do_POST(self):
        self._request("POST")

    def do_PUT(self):
        self._request("PUT")


def _root(home):
    return home.root / "dashboard"


def make_server(home, directory, port, token):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.dashboard = Dashboard(home, directory, token)
    return server


def serve(home, directory, port, *, announce=False):
    root = _root(home)
    state_file = root / "service.json"
    with file_lock(root / "server.lock", message="A dashboard service is already running."):
        token = secrets.token_hex(32)
        try:
            server = make_server(home, directory, port, token)
        except OSError as exc:
            raise ForgeError(f"Cannot listen on loopback port {port}: {exc}") from exc
        with server:
            record = {"pid": os.getpid(), "port": server.server_port, "token": token, "library_path": str(directory)}
            write_json(state_file, record)
            signal.signal(signal.SIGTERM, _interrupt)
            try:
                if announce:
                    print(_public(record)["url"], flush=True)
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                state_file.unlink(missing_ok=True)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _live(home):
    path = _root(home) / "service.json"
    if not path.is_file():
        return None
    record = read_json(path)
    http = HTTPConnection("127.0.0.1", record["port"], timeout=0.75)
    try:
        http.request("GET", "/api/service", headers={"X-Jforge-Token": record["token"]})
        response = http.getresponse()
        if response.status == HTTPStatus.OK and json.loads(response.read()).get("pid") == record["pid"]:
            return record
    except (OSError, ValueError):
        pass
    finally:
        http.close()
    return None


def _public(record):
    return {"pid": record["pid"], "port": record["port"], "library_path": record["library_path"],
            "url": f"http://127.0.0.1:{record['port']}/?token={record['token']}"}


def status(home):
    record = _live(home)
    if not record:
        raise ForgeError("Dashboard is stopped.", code=1, hint="jforge dashboard start")
    return _public(record)


def start(home, directory, port, *, foreground=False):
    home.require_initialized()
    root = _root(home)
    with file_lock(root / "start.lock", blocking=True):
        record = _live(home)
        if record:
            if record["library_path"] != str(directory):
                raise ForgeError("Dashboard is already serving a different library.", hint="jforge dashboard stop")
            return _public(record)
        if not foreground:
            with (root / "server.log").open("wb") as log:
                child = subprocess.Popen([sys.executable, "-m", "jarvis_forge.dashboard.server",
                                          str(home.root), str(directory), str(port)],
                                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            for _ in range(60):
                record = _live(home)
                if record and record["pid"] == child.pid:
                    return _public(record)
                if child.poll() is not None:
                    break
                time.sleep(0.08)
            if child.poll() is None:
                child.terminate()
                child.wait()
            detail = (root / "server.log").read_text(errors="replace").strip()
            raise ForgeError(detail or "Dashboard did not start.")
    serve(home, directory, port, announce=True)
    return None


def stop(home):
    record = _live(home)
    if not record:
        return {"stopped": True}
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(60):
        if _live(home) is None:
            return {"stopped": True}
        time.sleep(0.05)
    raise ForgeError("Dashboard is still stopping.", code=1)


if __name__ == "__main__":
    try:
        serve(ForgeHome(Path(sys.argv[1])), Path(sys.argv[2]), int(sys.argv[3]))
    except ForgeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(exc.code)
