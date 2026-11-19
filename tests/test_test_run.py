"""jforge test run, summarize, status, history and report (TEST_CATALOG_DESIGN.md PR 2, 2b, 3, 5)."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
import yaml

from jarvis_forge import testing, testing_run
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome


INPUTS = ["src/**/*.py", "tests/**/*.py", "pyproject.toml"]


@pytest.fixture
def home(tmp_path):
    value = ForgeHome(tmp_path / "home")
    value.initialize()
    return value


def git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=root,
                   check=True, capture_output=True)


def commit(root, message="change"):
    git(root, "add", "-A")
    git(root, "commit", "-qm", message, "--allow-empty")


def python_shell(tmp_path, python=sys.executable):
    """A shell activation whose python3 is the given interpreter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    wrapper = bin_dir / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec {python} "$@"\n')
    wrapper.chmod(0o755)
    shell = tmp_path / "env.sh"
    shell.write_text(f'export PATH="{bin_dir}:$PATH"\n')
    return str(shell)


def project(tmp_path, tests=None, *, groups=None, checks=None, profiles=None, **fields):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "lib.py").write_text("VALUE = 1\n")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.0"\n'
                                         '[tool.pytest.ini_options]\nmarkers = ["slow: slow"]\n')
    (root / ".gitignore").write_text("deps/\n")
    for name, text in (tests or {"test_a.py": "def test_ok():\n    pass\n"}).items():
        (root / "tests" / name).write_text(text)
    config = {"schema_version": 2, "kind": "checker", "id": "demo", "project": "..",
              "environment": {"shell": python_shell(tmp_path)}, **fields}
    if profiles:
        config["profiles"] = profiles
        config["default_profile"] = next(iter(profiles))
    if checks:
        config["checks"] = checks
    config["groups"] = groups if groups is not None else [
        {"id": "unit", "match": ["tests/test_*.py"], "inputs": INPUTS, "skip_allow": []}]
    if not config["groups"]:
        del config["groups"]
    path = root / "tests" / "jforge-tests.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    git(root, "init", "-q")
    commit(root, "init")
    return root, path


def steps(result):
    return {step["id"]: step for step in result["runs"][0]["steps"]}


def slot(home, path, group_id, key="1/1"):
    card = testing.load_card(path)
    state = testing_run.read_state(home, testing_run.card_key(card, None))
    return state["groups"].get(group_id, {}).get("batches", {}).get(key)


def jforge(home, *args):
    return [sys.executable, "-m", "jarvis_forge", "--home", str(home.root), *args]


# Execution, status mapping and reuse

def test_run_counts_cases_and_daily_versus_gate_reuse(tmp_path, home):
    tests = {"test_a.py": "import pytest\n\ndef test_ok():\n    pass\n\n"
                          "@pytest.mark.xfail(reason='known bug')\ndef test_bug():\n    assert False\n\n"
                          "class TestT:\n    @pytest.mark.parametrize('x', [1, 2])\n    def test_p(self, x):\n        pass\n"}
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": ["tests/**/*.py"], "skip_allow": []}]
    root, path = project(tmp_path, tests, groups=groups, profiles={"daily": ["unit"], "acceptance": ["unit"]},
                         coverage={"paths": ["tests/test_*.py"]})
    first = testing_run.run(home, path, profile="acceptance")
    step = steps(first)["unit"]
    assert first["status"] == "passed" and step["status"] == "passed" and step["pytest_exit_code"] == 0
    assert step["counts"] == {"tests": 4, "passed": 3, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 1,
                              "consistent": True}
    assert first["summary"]["accepted"] is True and first["acceptance_id"] == first["runs"][0]["run_id"]
    run_dir = Path(first["runs"][0]["path"])
    nodes = [json.loads(line)["node"] for line in (run_dir / "cases.jsonl").read_text().splitlines()]
    assert "tests/test_a.py::TestT::test_p[2]" in nodes and "tests/test_a.py::test_bug" in nodes
    identity = json.loads((run_dir / "steps/unit/identity.json").read_text())
    assert str(root / "tests" / "conftest.py") not in identity["conftest"]
    assert all(str(run_dir) not in entry for entry in identity["sys_path"])
    assert (run_dir / "report.md").is_file() and json.loads((run_dir / "report.json").read_text())["status"] == "passed"
    assert (home.root / "checker/reports" / f"{first['acceptance_id']}.md").is_file()

    (root / "src" / "lib.py").write_text("VALUE = 2\n")  # Not a declared input.
    commit(root)
    daily = testing_run.run(home, path, profile="daily")
    assert steps(daily)["unit"]["status"] == "reused"
    assert daily["summary"]["accepted"] is False  # daily is not a gate profile
    gate = testing_run.run(home, path, profile="acceptance")
    assert steps(gate)["unit"]["status"] == "passed"
    again = testing_run.run(home, path, profile="acceptance")
    assert steps(again)["unit"]["status"] == "reused" and again["summary"]["accepted"] is True
    assert testing_run.run(home, path, profile="acceptance", full=True)["runs"][0]["executed"] == 1


def test_package_upgrade_invalidates_both_reuse_modes(tmp_path, home):
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": ["tests/**/*.py"], "pythonpath": ["deps"],
               "skip_allow": []}]
    root, path = project(tmp_path, groups=groups, profiles={"daily": ["unit"], "acceptance": ["unit"]},
                         coverage={"paths": ["tests/test_*.py"]})

    def install(version):
        for old in (root / "deps").glob("*.dist-info"):
            for file in old.iterdir():
                file.unlink()
            old.rmdir()
        info = root / "deps" / f"fakepkg-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: fakepkg\nVersion: {version}\n")

    install("1.0")
    assert steps(testing_run.run(home, path, profile="acceptance"))["unit"]["status"] == "passed"
    assert steps(testing_run.run(home, path, profile="daily"))["unit"]["status"] == "reused"
    assert steps(testing_run.run(home, path, profile="acceptance"))["unit"]["status"] == "reused"
    install("1.1")
    assert steps(testing_run.run(home, path, profile="daily"))["unit"]["status"] == "passed"
    install("1.2")
    assert steps(testing_run.run(home, path, profile="acceptance"))["unit"]["status"] == "passed"


def test_collection_error_fails_with_exit_1(tmp_path, home, capsys):
    root, path = project(tmp_path, {"test_a.py": "import missing_module_8841\n"})
    assert main(["--home", str(home.root), "--json", "test", "run", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    step = result["runs"][0]["steps"][0]
    assert step["status"] == "failed" and step["pytest_exit_code"] == 2
    assert step["reason"] == "collection error or pytest interruption"
    assert result["ok"] is False and result["error"]["type"] == "VerificationFailed"


@pytest.mark.parametrize("conftest, markers, reason", [
    ("", "not (", "pytest usage error"),
    ("def pytest_unconfigure(config):\n    import os\n    open(os.environ['JFORGE_STEP_DIR'] + '/junit.xml', 'w').write('<oops')\n",
     "", "junit unreadable"),
    ("def pytest_unconfigure(config):\n    import os, signal\n    os.kill(os.getpid(), signal.SIGSEGV)\n",
     "", "pytest ended by signal 11"),
])
def test_failures_never_write_success_slots(tmp_path, home, conftest, markers, reason):
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": INPUTS, "markers": markers}]
    root, path = project(tmp_path, groups=groups)
    (root / "tests" / "conftest.py").write_text(conftest)
    commit(root)
    result = testing_run.run(home, path)
    step = steps(result)["unit"]
    assert result["status"] == "failed" and step["status"] == "failed" and step["reason"].startswith(reason)
    assert slot(home, path, "unit") is None


def test_whole_slow_module_under_not_slow_selects_nothing(tmp_path, home):
    tests = {"test_slow.py": "import pytest\n\n@pytest.mark.slow\ndef test_long():\n    pass\n"}
    groups = [{"id": "fast", "match": ["tests/test_*.py"], "inputs": INPUTS, "markers": "not slow"}]
    _, path = project(tmp_path, tests, groups=groups)
    step = steps(testing_run.run(home, path))["fast"]
    assert step["status"] == "passed" and step["reason"] == "no tests selected" and step["pytest_exit_code"] == 5


def test_unlisted_skip_fails_the_group_but_xfail_does_not(tmp_path, home):
    tests = {"test_a.py": "import pytest\n\n@pytest.mark.skip(reason='needs a GPU')\ndef test_gpu():\n    pass\n\n"
                          "@pytest.mark.skip(reason='Redis is down')\ndef test_redis():\n    pass\n\n"
                          "@pytest.mark.xfail(reason='known')\ndef test_x():\n    assert False\n",
             "test_m.py": "import pytest\npytest.skip('GPU driver missing', allow_module_level=True)\n"}
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": INPUTS, "skip_allow": ["GPU"]}]
    _, path = project(tmp_path, tests, groups=groups)
    step = steps(testing_run.run(home, path))["unit"]
    assert step["status"] == "failed" and step["reason"] == "unexpected skip"
    assert [skip["node"] for skip in step["unexpected_skips"]] == ["tests/test_a.py::test_redis"]
    assert {skip["message"] for skip in step["skips"]} == {"needs a GPU", "Redis is down", "GPU driver missing"}
    assert step["counts"]["skipped"] == 3 and step["counts"]["xfailed"] == 1


def test_checks_only_card_runs_argv_checks(tmp_path, home, capsys):
    ok = {"id": "ok", "argv": ["${python}", "-c", "print('fine')"], "cache": False}
    bad = {"id": "bad", "argv": ["${python}", "-c", "raise SystemExit(1)"], "cache": False, "needs": ["ok"]}
    _, path = project(tmp_path, groups=[], checks=[ok])
    assert main(["--home", str(home.root), "--json", "test", "run", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["steps"][0]["status"] == "passed"
    _, path = project(tmp_path / "second", groups=[], checks=[ok, bad])
    assert main(["--home", str(home.root), "--json", "test", "run", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "test run" and steps(result)["bad"]["status"] == "failed"


# Preflight

def test_preflight_errors_exit_2_before_any_run(tmp_path, home, capsys):
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": INPUTS}]
    _, path = project(tmp_path, groups=groups, profiles={"daily": ["unit"]})
    for args, message in ((["--batch", "2/4"], "need --acceptance-id"),
                          (["--profile", "daily", "--group", "unit"], "mutually exclusive"),
                          (["--project", "x"], "catalogs only"),
                          (["--env", "missing-env-1234"], "missing-env-1234")):
        assert main(["--home", str(home.root), "--json", "test", "run", str(path), *args]) == 2
        assert message in json.loads(capsys.readouterr().out)["error"]["message"]
    assert not (home.root / "runs").exists() or not list((home.root / "runs").iterdir())


def test_gate_profile_rechecks_skip_allow_and_coverage(tmp_path, home):
    groups = [{"id": "unit", "match": ["tests/test_a.py"], "inputs": INPUTS}]
    _, path = project(tmp_path, {"test_a.py": "def test_a():\n    pass\n", "test_b.py": "def test_b():\n    pass\n"},
                      groups=groups, profiles={"acceptance": ["unit"]})
    with pytest.raises(ForgeError, match="must declare skip_allow"):
        testing_run.run(home, path, profile="acceptance")
    groups[0]["skip_allow"] = []
    card = yaml.safe_load(path.read_text())
    card["groups"] = groups
    path.write_text(yaml.safe_dump(card))
    with pytest.raises(ForgeError, match="must declare coverage"):
        testing_run.run(home, path, profile="acceptance")
    card["coverage"] = {"paths": ["tests/test_*.py"]}
    path.write_text(yaml.safe_dump(card))
    with pytest.raises(ForgeError, match="outside every acceptance group: tests/test_b.py"):
        testing_run.run(home, path, profile="acceptance")


# Locks, shards and concurrency

def _shard_files(count):
    """Pick file names so that shard 2 and shard 3 of count each get one known file."""
    picked = {}
    number = 0
    while len(picked) < 2:
        name = f"tests/test_{number}.py"
        index = testing.bucket(name, count) + 1
        if index in (2, 3) and index not in picked:
            picked[index] = name
        number += 1
    return picked


def test_shards_run_in_parallel_and_a_busy_step_exits_1(tmp_path, home):
    meet = tmp_path / "meet"
    meet.mkdir()
    picked = _shard_files(4)
    wait = ("import pathlib, time\nMEET = pathlib.Path({meet!r})\n\ndef test_meet():\n"
            "    (MEET / {mine!r}).write_text('x')\n    deadline = time.time() + 60\n"
            "    while not (MEET / {other!r}).exists():\n        assert time.time() < deadline\n        time.sleep(0.05)\n")
    tests = {Path(picked[2]).name: wait.format(meet=str(meet), mine="two", other="three"),
             Path(picked[3]).name: wait.format(meet=str(meet), mine="three", other="two")}
    _, path = project(tmp_path, tests)
    two = subprocess.Popen(jforge(home, "test", "run", str(path), "--batch", "2/4", "--acceptance-id", "shards"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while not (meet / "two").exists():
        assert time.time() < deadline and two.poll() is None
        time.sleep(0.05)
    busy = subprocess.run(jforge(home, "--json", "test", "run", str(path), "--batch", "2/4", "--acceptance-id", "x"),
                          capture_output=True, text=True)
    assert busy.returncode == 1
    assert json.loads(busy.stdout)["error"]["message"] == "Test step is busy: unit--2of4"
    three = subprocess.run(jforge(home, "test", "run", str(path), "--batch", "3/4", "--acceptance-id", "shards"),
                           capture_output=True, text=True, timeout=120)
    assert three.returncode == 0, three.stderr
    assert two.wait(timeout=120) == 0
    for index in (1, 4):
        assert subprocess.run(jforge(home, "test", "run", str(path), "--batch", f"{index}/4", "--acceptance-id", "shards"),
                              capture_output=True).returncode == 0
    report = testing_run.summarize(home, "shards")
    assert report["status"] == "passed" and report["accepted"] is False  # no gate profile
    assert report["entries"][0]["groups"]["unit"]["shards"] == 4
    assert (home.root / "checker/index.jsonl").read_text().count('"acceptance_id": "shards"') == 4


def test_shared_prerequisite_check_runs_once_for_concurrent_shards(tmp_path, home):
    counter = tmp_path / "count.txt"
    prep = {"id": "prep", "inputs": ["pyproject.toml"],
            "argv": ["${python}", "-c", f"import time; open({str(counter)!r}, 'a').write('x\\n'); time.sleep(1)"]}
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": INPUTS, "needs": ["prep"]}]
    _, path = project(tmp_path, {"test_a.py": "def test_a():\n    pass\n", "test_b.py": "def test_b():\n    pass\n"},
                      groups=groups, checks=[prep])
    processes = [subprocess.Popen(jforge(home, "test", "run", str(path), "--group", "unit", "--batch", f"{k}/2",
                                         "--acceptance-id", "shared"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for k in (1, 2)]
    assert [process.wait(timeout=120) for process in processes] == [0, 0]
    assert counter.read_text() == "x\n"
    statuses = sorted(step["status"] for request, result in testing_run.test_runs(home)
                      for step in result["steps"] if step["id"] == "prep")
    assert statuses == ["passed", "reused"]


def test_sigterm_interrupts_with_exit_130(tmp_path, home):
    started = tmp_path / "started"
    tests = {"test_a.py": f"import pathlib, time\n\ndef test_long():\n    pathlib.Path({str(started)!r}).write_text('x')\n"
                          "    time.sleep(60)\n"}
    _, path = project(tmp_path, tests)
    process = subprocess.Popen(jforge(home, "test", "run", str(path)), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while not started.exists():
        assert time.time() < deadline and process.poll() is None
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) == 130
    request, result = next(testing_run.test_runs(home))
    assert result["status"] == "interrupted" and result["steps"][0]["status"] == "interrupted"


def test_summarize_rejects_mixed_shard_sets(tmp_path, home):
    _, path = project(tmp_path, {"test_a.py": "def test_a():\n    pass\n", "test_b.py": "def test_b():\n    pass\n"})
    testing_run.run(home, path, batch="1/2", acceptance_id="mixed")
    testing_run.run(home, path, batch="1/3", acceptance_id="mixed")
    with pytest.raises(ForgeError, match="mixes shard sets") as error:
        testing_run.summarize(home, "mixed")
    assert error.value.code == 2
    testing_run.run(home, path, batch="1/2", acceptance_id="partial")
    report = testing_run.summarize(home, "partial")
    assert report["status"] == "failed" and "group unit misses shard(s) 2/2" in report["entries"][0]["reasons"]
    with pytest.raises(ForgeError, match="No test runs"):
        testing_run.summarize(home, "nothing-here")


# Status, history and report

def test_status_history_and_report(tmp_path, home, capsys):
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "inputs": INPUTS, "skip_allow": []}]
    root, path = project(tmp_path, groups=groups, profiles={"acceptance": ["unit"]},
                         coverage={"paths": ["tests/test_*.py"]})
    base = ["--home", str(home.root), "--json", "test"]
    assert main([*base, "status", str(path)]) == 1
    assert "No acceptance report" in json.loads(capsys.readouterr().out)["cards"][0]["reason"]
    result = testing_run.run(home, path, profile="acceptance")
    assert main([*base, "status", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["cards"][0]["acceptance_id"] == result["acceptance_id"]
    (root / "src" / "lib.py").write_text("VALUE = 3\n")
    assert main([*base, "status", str(path)]) == 1
    assert "uncommitted" in json.loads(capsys.readouterr().out)["cards"][0]["reason"]
    commit(root)
    assert main([*base, "status", str(path)]) == 1
    assert "source changed after" in json.loads(capsys.readouterr().out)["cards"][0]["reason"]

    assert main([*base, "history", "--case", "test_ok"]) == 0
    runs = json.loads(capsys.readouterr().out)["runs"]
    assert runs[0]["cases"][0]["node"] == "tests/test_a.py::test_ok"
    assert main([*base, "report", result["acceptance_id"]]) == 0  # Same name as the run: the run wins.
    assert json.loads(capsys.readouterr().out)["kind"] == "run"
    testing_run.run(home, path, profile="acceptance", acceptance_id="gate-1")
    testing_run.summarize(home, "gate-1")
    assert main([*base, "report", "gate-1"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["kind"] == "acceptance" and value["report"]["entries"][0]["accepted"] is True
    assert main(["--home", str(home.root), "test", "report", result["runs"][0]["run_id"], "--format", "markdown"]) == 0
    assert capsys.readouterr().out.startswith("# Test run ")
    assert main(["--home", str(home.root), "test", "summarize", "--acceptance-id", result["acceptance_id"], "--rebuild"]) == 0


def test_accept_discovery_and_newer_full_failure(tmp_path, home, capsys, monkeypatch):
    root, path = project(tmp_path, {"test_a.py": "from pathlib import Path\n"
                                              "def test_ok():\n    assert not Path('deps/fail').exists()\n"},
                         profiles={"acceptance": ["unit"]}, coverage={"paths": ["tests/test_*.py"]})
    monkeypatch.chdir(root / "tests")
    base = ["--home", str(home.root), "--json", "test"]
    assert main([*base, "accept"]) == 0
    first = json.loads(capsys.readouterr().out)["acceptance_id"]
    (root / "deps").mkdir()
    (root / "deps" / "fail").touch()
    assert main([*base, "run", "--group", "unit", "--full"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert main([*base, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["cards"][0]["acceptance_id"] == first
    assert main([*base, "accept", "--full"]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["cards"][0]["status"] == "failed"
    latest = value["acceptance_id"]
    testing_run.summarize(home, first)
    assert main([*base, "status"]) == 1
    assert json.loads(capsys.readouterr().out)["cards"][0]["acceptance_id"] == latest
    assert path.is_file()


def test_status_invalidates_group_dependencies_and_editable_contents(tmp_path, home):
    groups = [{"id": "unit", "match": ["tests/test_*.py"], "pythonpath": ["deps"], "skip_allow": [],
               "inputs": INPUTS}]
    root, path = project(tmp_path, groups=groups, profiles={"acceptance": ["unit"]},
                         coverage={"paths": ["tests/test_*.py"]})
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    module = dependency / "demo_dependency.py"
    module.write_text("VALUE = 1\n")
    git(dependency, "init", "-q")
    commit(dependency)
    info = root / "deps" / "demo_dependency-1.0.dist-info"
    info.mkdir(parents=True)
    metadata = info / "METADATA"
    metadata.write_text("Name: demo-dependency\nVersion: 1.0\n")
    (info / "direct_url.json").write_text(json.dumps({"url": dependency.as_uri(), "dir_info": {"editable": True}}))
    assert testing_run.accept(home, path)["accepted"]
    module.write_text("VALUE = 2\n")
    current = testing_run.status(home, path)["cards"][0]
    assert current["status"] == "stale" and "editable Python dependency" in current["reason"]
    module.write_text("VALUE = 1\n")
    metadata.write_text("Name: demo-dependency\nVersion: 2.0\n")
    current = testing_run.status(home, path)["cards"][0]
    assert current["status"] == "stale" and "Installed Python distributions" in current["reason"]


def test_status_invalidates_same_named_environment_at_a_new_revision(tmp_path, home):
    from jarvis_forge import environment_registry

    _, path = project(tmp_path, profiles={"acceptance": ["unit"]}, coverage={"paths": ["tests/test_*.py"]})
    shell = tmp_path / "env.sh"
    environment_registry.add(home, "science", shell)
    assert testing_run.accept(home, path, environment_name="science")["accepted"]
    shell.write_text(shell.read_text() + "# environment revision change\n")
    environment_registry.add(home, "science", shell, replace=True)
    current = testing_run.status(home, path, environment_name="science")["cards"][0]
    assert current["status"] == "stale" and "its revision changed" in current["reason"]


def test_archive_gate_verifies_project_and_pinned_archive(tmp_path, home):
    import tarfile
    from jarvis_forge.storage import digest

    root = tmp_path / "Extracted"
    root.mkdir()
    sample = root / "sample.txt"
    sample.write_text("original\n")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(root, arcname=root.name)
    card = tmp_path / "jforge-tests.yaml"
    card.write_text(yaml.safe_dump({
        "schema_version": 2, "kind": "checker", "id": "demo-archive", "project": root.name,
        "subject": {"archive": archive.name, "sha256": digest(archive)},
        "environment": {"shell": python_shell(tmp_path)},
        "default_profile": "acceptance", "profiles": {"acceptance": ["sample"]},
        "checks": [{"id": "sample", "argv": ["${python}", "-c", "print('verified')"], "cache": False}],
    }))
    assert testing_run.accept(home, card)["accepted"]
    sample.write_text("modified\n")
    current = testing_run.status(home, card)["cards"][0]
    assert current["status"] == "stale" and "sample.txt" in current["reason"]
    assert testing_run.accept(home, card)["runs"][0]["steps"] == []
    sample.write_text("original\n")
    with archive.open("ab") as stream:
        stream.write(b"tampered")
    assert "differs from the pinned digest" in testing_run.status(home, card)["cards"][0]["reason"]


# Evidence parsing and argv

def test_parse_junit_needs_files_and_rebuilds_empty_classnames(tmp_path):
    project_dir = tmp_path / "p"
    (project_dir / "tests").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    junit = tmp_path / "junit.xml"
    rel = os.path.relpath(project_dir / "tests" / "test_x.py", work)
    junit.write_text(f'<testsuites><testsuite tests="2" failures="0" errors="1" skipped="0">'
                     f'<testcase classname="" name="tests.test_x" file="{rel}"><error message="collection failure"/></testcase>'
                     f'<testcase classname="x.tests.test_x.TestA" name="test_b" file="{rel}" time="0.5"/>'
                     f'</testsuite></testsuites>')
    counts, cases, problems = testing_run.parse_junit(junit, project_dir, work, step="s", rewrite=True)
    assert problems == [] and counts["errors"] == 1 and counts["passed"] == 1
    assert [case["node"] for case in cases] == ["tests/test_x.py", "tests/test_x.py::TestA::test_b"]
    assert 'classname="tests.test_x"' in junit.read_text()
    junit.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0">'
                     '<testcase classname="tests.test_x" name="test_a"/></testsuite>')
    _, _, problems = testing_run.parse_junit(junit, project_dir, project_dir)
    assert problems and problems[0].startswith("junit testcase without a file")
    assert testing_run.pytest_status(0, {"tests": 1}, problems)[0] == "failed"
    junit.write_text(f'<testsuite tests="3" failures="0" errors="0" skipped="1">'
                     f'<testcase classname="" name="test_a" file="{rel}"/>'
                     f'<testcase classname=".TestK" name="test_b[1]" file="{rel}"/>'
                     f'<testcase classname="" name="" file="{rel}"><skipped message="collection skipped">'
                     f"('/x/tests/test_x.py', 2, 'Skipped: needs a GPU')</skipped></testcase></testsuite>")
    counts, cases, problems = testing_run.parse_junit(junit, project_dir, work)
    assert problems == [] and counts["skipped"] == 1
    assert [(case["node"], case["message"]) for case in cases] == [
        ("tests/test_x.py::test_a", None), ("tests/test_x.py::TestK::test_b[1]", None), ("tests/test_x.py", "needs a GPU")]


def test_pytest_argv_is_assembled_by_the_executor(tmp_path):
    group = testing._group({"id": "g", "match": ["tests/*.py"], "inputs": ["x"], "markers": "not slow", "workers": 2,
                            "pytest_args": ["-vv", "--tb", "line", "--maxfail=3"]})
    argv = testing_run.pytest_argv("py", group, tmp_path, ["tests/b.py", "tests/a.py"], Path("/p"))
    assert argv[:5] == ["py", "-m", "pytest", "-vv", "--tb=line"]
    assert ["-m", "not slow"] == argv[argv.index("-m", 3):argv.index("-m", 3) + 2]
    assert argv[-3:] == ["--maxfail=3", "tests/a.py", "tests/b.py"] and "-n" in argv
    assert "--override-ini=junit_family=xunit1" in argv and not any(a.startswith("--rootdir") for a in argv)
    installed = testing._group({"id": "i", "match": ["tests/*.py"], "inputs": ["x"], "isolate": "installed",
                                "distributions": ["demo"]})
    argv = testing_run.pytest_argv("py", installed, tmp_path, ["tests/a.py"], Path("/p"))
    assert f"--rootdir={tmp_path / 'work'}" in argv and "--override-ini=addopts=" in argv
    assert "--import-mode=importlib" in argv and argv[-1] == "/p/tests/a.py"


# --rerun-failed

def test_rerun_failed_runs_only_what_did_not_pass(tmp_path, home, capsys):
    tests = {"test_a.py": "import pytest\nfrom lib import VALUE\n\ndef test_ok():\n    pass\n\n"
                          "def test_bad():\n    assert VALUE == 2\n\n"
                          "class TestK:\n    @pytest.mark.parametrize('x', [1, 2])\n"
                          "    def test_p(self, x):\n        assert x == 2 or VALUE == 2\n",
             "test_b.py": "def test_fine():\n    pass\n"}
    groups = [{"id": "unit", "match": ["tests/test_a.py"], "inputs": INPUTS, "pythonpath": ["src"], "skip_allow": []},
              {"id": "other", "match": ["tests/test_b.py"], "inputs": INPUTS, "skip_allow": []}]
    check = {"id": "ok", "argv": ["${python}", "-c", "print('fine')"], "cache": False}
    root, path = project(tmp_path, tests, groups=groups, checks=[check],
                         profiles={"daily": ["unit", "other", "ok"]})
    cli = ["--home", str(home.root), "--json", "test", "run", str(path)]
    assert main([*cli, "--rerun-failed", "checker-missing"]) == 2
    assert "No test run checker-missing" in json.loads(capsys.readouterr().out)["error"]["message"]

    first = testing_run.run(home, path)
    first_id = first["runs"][0]["run_id"]
    assert steps(first)["unit"]["status"] == "failed" and steps(first)["unit"]["counts"]["failed"] == 2
    other_slot = slot(home, path, "other")
    assert other_slot is not None
    for args in (["--full"], ["--acceptance-id", "a1"], ["--profile", "daily"], ["--batch", "1/2"]):
        assert main([*cli, "--rerun-failed", first_id, *args]) == 2
        assert "does not combine" in json.loads(capsys.readouterr().out)["error"]["message"]
    _, elsewhere = project(tmp_path / "elsewhere", tests, id="other-card")
    assert main(["--home", str(home.root), "--json", "test", "run", str(elsewhere), "--rerun-failed", first_id]) == 2
    assert "belongs to card demo" in json.loads(capsys.readouterr().out)["error"]["message"]

    (root / "src" / "lib.py").write_text("VALUE = 2\n")
    rerun = testing_run.run(home, path, rerun_failed=first_id)
    record = rerun["runs"][0]
    unit, other, ok = (steps(rerun)[name] for name in ("unit", "other", "ok"))
    assert rerun["status"] == "passed" and record["partial"] is True and record["rerun_of"] == first_id
    assert unit["status"] == "passed" and unit["counts"]["tests"] == 2
    assert unit["rerun"] == {"from": first_id,
                             "nodes": ["tests/test_a.py::TestK::test_p[1]", "tests/test_a.py::test_bad"]}
    assert other["status"] == "reused" and other["reused_from"] == first_id and ok["status"] == "reused"
    assert rerun["acceptance_id"] is None and rerun["summary"] is None
    assert slot(home, path, "unit") is None and slot(home, path, "other") == other_slot
    report = json.loads((Path(record["path"]) / "report.json").read_text())
    assert report["partial"] is True and "Partial: rerun" in (Path(record["path"]) / "report.md").read_text()
    index = [json.loads(line) for line in (home.root / "checker/index.jsonl").read_text().splitlines()]
    assert index[-1]["partial"] is True and index[-1]["rerun_of"] == first_id

    again = testing_run.run(home, path, rerun_failed=record["run_id"])
    assert {step["status"] for step in again["runs"][0]["steps"]} == {"reused"}
    assert steps(again)["unit"]["reused_from"] == record["run_id"]
    assert steps(again)["other"]["reused_from"] == first_id


def test_rerun_failed_runs_the_whole_shard_after_a_collection_error(tmp_path, home):
    root, path = project(tmp_path, {"test_a.py": "import missing_module_8841\n"})
    first = testing_run.run(home, path)
    assert steps(first)["unit"]["pytest_exit_code"] == 2
    (root / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n\ndef test_two():\n    pass\n")
    step = steps(testing_run.run(home, path, rerun_failed=first["runs"][0]["run_id"]))["unit"]
    assert step["status"] == "passed" and step["rerun"]["nodes"] is None and step["counts"]["tests"] == 2


# Installed isolation and the package check

@pytest.fixture
def installed(tmp_path):
    """A venv that sees the current pytest and holds a non-editable fake demo_pkg."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", "--without-pip", str(venv)], check=True)
    python = venv / "bin" / "python"
    probe = subprocess.run([str(python), "-c", "import pytest, sysconfig; print(sysconfig.get_paths()['purelib'])"],
                           capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("The base interpreter does not provide pytest to a --system-site-packages venv.")
    site = Path(probe.stdout.strip())
    (site / "demo_pkg").mkdir()
    (site / "demo_pkg" / "__init__.py").write_text("WHERE = 'installed'\n")
    info = site / "demo_pkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo_pkg\nVersion: 1.0\n")
    (info / "top_level.txt").write_text("demo_pkg\n")
    (info / "RECORD").write_text("demo_pkg/__init__.py,,\ndemo_pkg-1.0.dist-info/METADATA,,\n")
    return {"python": python, "site": site, "info": info}


INSTALLED_TEST = ("import demo_pkg\nfrom helper import VALUE\n\n"
                  "def test_installed():\n    assert demo_pkg.WHERE == 'installed' and VALUE == 7\n\n"
                  "class TestK:\n    def test_k(self):\n        pass\n")


def installed_project(tmp_path, venv, tests=None, checks=None, groups=None):
    root, path = project(tmp_path, tests or {"test_inst.py": INSTALLED_TEST, "helper.py": "VALUE = 7\n",
                                             "conftest.py": "LOADED = True\n"},
                         groups=[{"id": "inst", "isolate": "installed", "import_mode": "prepend",
                                  "match": ["tests/test_*.py"], "inputs": ["pyproject.toml"],
                                  "distributions": ["demo_pkg"]}] if groups is None else groups,
                         checks=checks)
    (root / "src" / "demo_pkg").mkdir()
    (root / "src" / "demo_pkg" / "__init__.py").write_text("WHERE = 'source'\n")
    card = yaml.safe_load(path.read_text())
    card["environment"] = {"shell": python_shell(tmp_path, venv["python"])}
    path.write_text(yaml.safe_dump(card))
    commit(root)
    return root, path


def test_installed_group_imports_the_installed_package(tmp_path, home, installed, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "repo" / "src"))  # Installed mode must drop it.
    root, path = installed_project(tmp_path, installed)
    result = testing_run.run(home, path)
    step = steps(result)["inst"]
    assert step["status"] == "passed", step
    assert step["pythonpath_cleared"] is True and step["pythonpath"] == ""
    run_dir = Path(result["runs"][0]["path"])
    identity = json.loads((run_dir / "steps/inst/identity.json").read_text())
    assert str(root / "tests" / "conftest.py") in identity["conftest"]
    assert str(root) not in identity["sys_path"] and str(root / "src") not in identity["sys_path"]
    cases = [json.loads(line) for line in (run_dir / "cases.jsonl").read_text().splitlines()]
    assert [case["node"] for case in cases] == ["tests/test_inst.py::test_installed", "tests/test_inst.py::TestK::test_k"]
    assert 'classname="tests.test_inst.TestK"' in (run_dir / "steps/inst/junit.xml").read_text()
    assert steps(testing_run.run(home, path))["inst"]["status"] == "reused"

    (root / "tests" / "test_broken.py").write_text("import missing_module_5521\n")
    commit(root)
    result = testing_run.run(home, path)
    step = steps(result)["inst"]
    assert step["status"] == "failed" and step["reason"] == "collection error or pytest interruption"
    cases = [json.loads(line) for line in (Path(result["runs"][0]["path"]) / "cases.jsonl").read_text().splitlines()]
    assert {"node": "tests/test_broken.py", "outcome": "error"}.items() <= cases[0].items()
    assert json.loads((run_dir / "steps/inst/distributions.json").read_text())["distributions"][0]["found"] is True


def test_installed_group_rejects_editable_and_source_imports(tmp_path, home, installed):
    (installed["info"] / "direct_url.json").write_text(json.dumps({"url": "file:///x", "dir_info": {"editable": True}}))
    _, path = installed_project(tmp_path, installed)
    step = steps(testing_run.run(home, path))["inst"]
    assert step["status"] == "failed" and step["reason"] == "distribution demo_pkg is an editable install"
    (installed["info"] / "direct_url.json").unlink()
    leak = ("import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n"
            "import demo_pkg as pkg\nimport importlib\nimportlib.reload(pkg)\n\ndef test_leak():\n    pass\n")
    _, path = installed_project(tmp_path / "leak", installed, tests={"test_leak.py": leak})
    step = steps(testing_run.run(home, path))["inst"]
    assert step["status"] == "failed" and step["reason"].startswith("source tree imported: demo_pkg")


def test_package_check_probes_the_installed_distribution(tmp_path, home, installed):
    check = {"id": "wheel", "check": {"package": {"distribution": "demo_pkg", "modules": ["demo_pkg"],
                                                  "commands": [["python3", "-c", "import demo_pkg"]]}}}
    root, path = installed_project(tmp_path, installed, groups=[], checks=[check])
    assert steps(testing_run.run(home, path))["wheel"]["status"] == "passed"
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "2.0"\n')
    commit(root)
    step = steps(testing_run.run(home, path))["wheel"]
    assert step["status"] == "failed" and "differs from source version 2.0" in step["error"]
    (installed["info"] / "direct_url.json").write_text(json.dumps({"url": "file:///x", "dir_info": {"editable": True}}))
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.0"\n')
    commit(root)
    step = steps(testing_run.run(home, path))["wheel"]
    assert step["status"] == "failed" and "editable install" in step["error"]


def test_rerun_failed_passes_absolute_test_ids_in_installed_mode(tmp_path, home, installed):
    tests = {"test_inst.py": "import demo_pkg\nfrom helper import VALUE\n\ndef test_ok():\n    pass\n\n"
                             "class TestK:\n    def test_k(self):\n        assert VALUE == 8\n",
             "helper.py": "VALUE = 7\n"}
    root, path = installed_project(tmp_path, installed, tests=tests)
    first = testing_run.run(home, path)
    assert steps(first)["inst"]["counts"]["failed"] == 1
    (root / "tests" / "helper.py").write_text("VALUE = 8\n")
    step = steps(testing_run.run(home, path, rerun_failed=first["runs"][0]["run_id"]))["inst"]
    assert step["status"] == "passed", step
    assert step["counts"]["tests"] == 1 and step["rerun"]["nodes"] == ["tests/test_inst.py::TestK::test_k"]
