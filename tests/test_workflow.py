from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest
import yaml

from jarvis_forge.cli import main
from jarvis_forge.comparison import compare
from jarvis_forge.runner import execute
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json
from jarvis_forge.suite import load_suite, make_plan


@pytest.fixture
def home(tmp_path):
    home = ForgeHome(tmp_path / "home")
    home.initialize()
    return home


def suite(tmp_path, steps, **extra):
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "id": "acceptance", "steps": steps, **extra}))
    return path


def command(step_id="produce", code="print('hello')", **extra):
    return {"id": step_id, "argv": [sys.executable, "-c", code], **extra}


def test_two_runs_archive_outputs_and_compare(home, tmp_path):
    source = suite(tmp_path, [command(code="from pathlib import Path; Path('output.txt').write_text('original')",
                                      artifacts=["output.txt"])])
    first = execute(home, source)
    second = execute(home, source)
    assert first["run_id"] != second["run_id"]
    assert first["status"] == second["status"] == "passed"
    (Path(first["path"]) / "workspace/output.txt").write_text("changed later")
    assert Path(first["steps"][0]["artifacts"]["output.txt"]["snapshot"]).read_text() == "original"
    result = compare(home, first["run_id"], second["run_id"])
    assert result["status"] == "equal" and result["check_passed"]
    assert len(home.history("acceptance")) == 2
    assert read_json(Path(first["path"]) / "result.json")["status"] == "passed"


def test_changed_command_preserves_old_snapshot(home, tmp_path):
    source = suite(tmp_path, [command(code="print(1)")])
    first = execute(home, source)
    old_text = (Path(first["path"]) / "suite.snapshot.yaml").read_text()
    source = suite(tmp_path, [command(code="print(2)")])
    second = execute(home, source)
    result = compare(home, first["run_id"], second["run_id"])
    assert result["status"] == "different"
    assert result["steps"][0]["command_changed"]
    assert (Path(first["path"]) / "suite.snapshot.yaml").read_text() == old_text


def test_artifact_tampering_is_not_comparable(home, tmp_path):
    source = suite(tmp_path, [command(code="from pathlib import Path; Path('x').write_text('x')", artifacts=["x"])])
    a, b = execute(home, source), execute(home, source)
    Path(a["steps"][0]["artifacts"]["x"]["snapshot"]).write_text("tampered")
    result = compare(home, a["run_id"], b["run_id"])
    assert result["status"] == "not_comparable" and not result["check_passed"]


def test_failure_blocks_dependents_but_runs_independent_steps(home, tmp_path):
    source = suite(tmp_path, [command("bad", "raise SystemExit(7)"),
                              command("blocked", needs=["bad"]), command("independent")])
    result = execute(home, source)
    statuses = {s["id"]: s["status"] for s in result["steps"]}
    assert statuses == {"bad": "failed", "blocked": "blocked", "independent": "passed"}
    assert result["steps"][0]["exit_code"] == 7
    assert result["status"] == "failed"


def test_timeout_stops_children_before_they_write(home, tmp_path):
    marker = tmp_path / "late-child"
    child = "import time; from pathlib import Path; time.sleep(0.8); Path(" + repr(str(marker)) + ").write_text('bad')"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(5)"
    source = suite(tmp_path, [command(code=parent, timeout_seconds=0.2)])
    result = execute(home, source)
    assert result["steps"][0]["reason"] == "timeout"
    import time
    time.sleep(1)
    assert not marker.exists()


def test_environment_is_loaded_in_same_shell_and_snapshot_saved(home, tmp_path):
    script = tmp_path / "env with spaces.sh"
    script.write_text("export FORGE_TEST_VALUE='loaded'\nprintf 'activation output\\n'\n")
    source = suite(tmp_path, [command(code="import os; assert os.environ['FORGE_TEST_VALUE']=='loaded'")],
                   environment={"shell": script.name})
    result = execute(home, source)
    assert result["status"] == "passed"
    assert (Path(result["path"]) / "environment.sh").read_text() == script.read_text()
    assert "activation output" in (Path(result["path"]) / "steps/produce/stdout.log").read_text()


def test_literal_argv_is_not_interpreted_by_shell(home, tmp_path):
    marker = tmp_path / "should-not-exist"
    literal = f"$(touch {marker}) ; echo unexpected"
    source = suite(tmp_path, [{"id": "literal", "argv": [sys.executable, "-c",
                   "import sys; assert sys.argv[1] == " + repr(literal), literal]}])
    assert execute(home, source)["status"] == "passed"
    assert not marker.exists()


def test_inputs_are_copied_and_placeholders_resolved(home, tmp_path):
    original = tmp_path / "input.txt"
    original.write_text("input")
    source = suite(tmp_path, [{"id": "read", "argv": ["${python}", "-c",
                   "import sys; from pathlib import Path; assert Path(sys.argv[1]).read_text()=='input'",
                   "${input:data}"]}], inputs={"data": "input.txt"})
    result = execute(home, source)
    assert result["status"] == "passed"
    original.write_text("later")
    assert (Path(result["path"]) / "inputs/data/input.txt").read_text() == "input"


@pytest.mark.parametrize("artifact", ["../outside", "/tmp/outside", "."])
def test_invalid_artifact_paths_rejected_before_running(tmp_path, artifact):
    source = suite(tmp_path, [command(artifacts=[artifact])])
    with pytest.raises(ForgeError):
        load_suite(source)


def test_artifact_symlink_escape_fails(home, tmp_path):
    secret = tmp_path / "external"
    secret.write_text("outside")
    source = suite(tmp_path, [command(code="from pathlib import Path; Path('link').symlink_to(" + repr(str(secret)) + ")",
                                      artifacts=["link"])])
    result = execute(home, source)
    assert result["status"] == "failed"
    assert not result["steps"][0]["artifacts"]


@pytest.mark.parametrize("steps", [
    [command("x"), command("x")],
    [command("x", needs=["missing"])],
    [command("x", needs=["y"]), command("y", needs=["x"])],
    [command("x", timeout_seconds=float("nan"))],
])
def test_bad_dependency_or_timeout_rejected(tmp_path, steps):
    with pytest.raises(ForgeError):
        load_suite(suite(tmp_path, steps))


def test_duplicate_yaml_keys_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("schema_version: 1\nid: first\nid: second\nsteps: []\n")
    with pytest.raises(ForgeError, match="Duplicate"):
        load_suite(path)


def test_plan_and_queries_do_not_create_home(tmp_path):
    home = ForgeHome(tmp_path / "absent")
    source = suite(tmp_path, [command()])
    make_plan(load_suite(source), home.root / "runs/planned-run")
    assert home.history() == [] and home.packages() == []
    assert not home.root.exists()


def test_init_preserves_existing_identity(home):
    identity = home.require_initialized()
    assert home.initialize() == identity


def test_unknown_record_is_not_reported_as_passed(home):
    path = home.root / "runs" / "unfinished"
    write_json(path / "request.json", {"run_id": "unfinished", "suite_id": "acceptance"})
    assert home.show("unfinished")["status"] == "unknown"


def test_incomplete_runs_are_not_comparable(home):
    for run_id in ("incomplete-a", "incomplete-b"):
        write_json(home.root / "runs" / run_id / "request.json",
                   {"run_id": run_id, "suite_id": "acceptance"})
    result = compare(home, "incomplete-a", "incomplete-b")
    assert result["status"] == "not_comparable" and result["missing_records"]


def test_interrupt_preserves_result_and_stops_child(home, tmp_path):
    import signal
    import subprocess
    import time
    marker = tmp_path / "started"
    source = suite(tmp_path, [command(code="from pathlib import Path; import time; Path(" +
                          repr(str(marker)) + ").write_text('ready'); time.sleep(30)")])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen([sys.executable, "-m", "jarvis_forge", "--home", str(home.root),
                                "run", str(source), "--json"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), "Child did not start"
        process.send_signal(signal.SIGINT)
        stdout, _ = process.communicate(timeout=5)
        result = json.loads(stdout)
        assert process.returncode == 130
        assert result["status"] == result["steps"][0]["status"] == "interrupted"
        assert home.show(result["run_id"])["status"] == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

