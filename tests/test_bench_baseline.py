"""Baselines, run comparison, trend, rerun-failed and data prune."""

import json
from pathlib import Path
import sys

import pytest

from jarvis_forge import bench
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome


@pytest.fixture
def home(tmp_path):
    value = ForgeHome(tmp_path / "home")
    value.initialize()
    return value


def _item(result, name):
    items = [item for step in result["cases"][0]["steps"] for item in (step.get("items") or [])]
    return next(item for item in items if item["name"] == name)


def _perf(root, worker=10, helper=1):
    worker_dir = root / "src" / "worker"
    helper_dir = root / "src" / "helper"
    worker_dir.mkdir(parents=True, exist_ok=True)
    helper_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "version.txt").write_text(f"{worker}\n")
    (helper_dir / "version.txt").write_text(f"{helper}\n")
    (root / "measure.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "value = float(Path(os.environ['WORKER']).read_text())\n"
        "Path('metrics.json').write_text(json.dumps({'wall_seconds': value, 'evaluations': 100}))\n")
    case = root / "cases" / "alpha"
    case.mkdir(parents=True, exist_ok=True)
    (case / "case.yaml").write_text("{}\n")
    python = json.dumps(sys.executable)
    script = json.dumps(str(root / "measure.py"))
    card = root / "bench.yaml"
    card.write_text(f"""kind: bench
name: perf
subject: worker
software:
  worker: {{source: src/worker}}
  helper: {{source: src/helper}}
cases: cases/*
steps:
  - name: measure
    software: [worker, helper]
    env:
      WORKER: "${{software.worker.path}}/version.txt"
    commands:
      - [{python}, {script}]
    outputs: {{metrics: metrics.json}}
  - name: speed
    after: [measure]
    compare:
      actual: ${{measure.outputs.metrics}}
      select: [wall_seconds, evaluations]
      expected: baseline
      tolerance: {{max_ratio: 1.15}}
default_profile: quick
profiles:
  quick: {{}}
""")
    return card


def _retry(root, bad=100):
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "marker.txt").write_text("tool\n")
    (root / "reference.yaml").write_text("values:\n  value: 1\n")
    for name, number in (("good", 1), ("bad", bad)):
        case = root / "cases" / name
        case.mkdir(parents=True, exist_ok=True)
        (case / "case.yaml").write_text("{}\n")
        (case / "marker").write_text(f"{number}\n")
    python = json.dumps(sys.executable)
    card = root / "bench.yaml"
    card.write_text(f"""kind: bench
name: retry
software:
  tool: {{source: src}}
cases: cases/*
steps:
  - name: work
    software: [tool]
    commands:
      - [{python}, "-c", "import json,os; from pathlib import Path; n=float(Path(os.environ['CASE_DIR'], 'marker').read_text()); json.dump({{'value': n}}, open('result.json','w'))"]
    outputs: {{result: result.json}}
  - name: check
    after: [work]
    compare:
      actual: ${{work.outputs.result}}
      select: [value]
      expected: ${{bench.dir}}/reference.yaml
      tolerance: {{abs: 0.5}}
default_profile: all
profiles:
  all: {{}}
""")
    return card


def test_baseline_follows_subject_software_and_trend_lists_it(home, tmp_path):
    root = tmp_path / "work"
    card = _perf(root, worker=10)
    first = bench.run(home, card)
    assert first["status"] == "reported"
    assert _item(first, "wall_seconds")["reason"] == "no baseline"
    assert _item(first, "wall_seconds")["actual"] == 10

    card = _perf(root, worker=11)
    second = bench.run(home, card)
    assert second["status"] == "passed"
    assert _item(second, "wall_seconds")["expected"] == 10
    assert _item(second, "wall_seconds")["status"] == "passed"

    card = _perf(root, worker=11, helper=2)
    third = bench.run(home, card)
    assert third["status"] == "reported"
    assert "helper" in _item(third, "wall_seconds")["reason"]

    same = bench.compare_runs(home, first["run_id"], second["run_id"])
    assert same["comparable"]
    wall = next(row for row in same["rows"] if row["item"] == "wall_seconds")
    assert wall["a"] == 10 and wall["b"] == 11
    drifted = bench.compare_runs(home, second["run_id"], third["run_id"])
    assert not drifted["comparable"]
    assert any("helper" in change["name"] for change in drifted["changes"])

    series = bench.trend(home, card, case="alpha", step="speed", item="wall_seconds")
    assert [row["actual"] for row in series["rows"]] == [10, 11, 11]
    assert series["rows"][0]["software"] != series["rows"][1]["software"]
    assert series["rows"][1]["software"] == series["rows"][2]["software"]


def test_explicit_baseline_overrides_the_latest_accepted_run(home, tmp_path):
    root = tmp_path / "work"
    card = _perf(root, worker=10)
    first = bench.run(home, card)
    _perf(root, worker=11)
    second = bench.run(home, card)
    assert _item(second, "wall_seconds")["expected"] == 10
    _perf(root, worker=12)
    pinned = bench.run(home, card, baseline=first["run_id"])
    assert _item(pinned, "wall_seconds")["expected"] == 10
    assert _item(pinned, "wall_seconds")["status"] == "failed"
    with pytest.raises(ForgeError, match="not found"):
        bench.run(home, card, baseline="bench-missing")


def test_baseline_stays_on_the_same_machine_class(home, tmp_path):
    root = tmp_path / "work"
    card = _perf(root, worker=10)
    (home.root / "executors").mkdir()
    (home.root / "executors" / "local.yaml").write_text("machine_class: alpha\n")
    first = bench.run(home, card)
    assert _item(first, "wall_seconds")["reason"] == "no baseline"
    (home.root / "executors" / "local.yaml").write_text("machine_class: beta\n")
    other = bench.run(home, card)
    assert "machine class" in _item(other, "wall_seconds")["reason"]
    (home.root / "executors" / "local.yaml").write_text("machine_class: alpha\n")
    card = _perf(root, worker=11)
    again = bench.run(home, card)
    assert again["status"] == "passed"
    assert _item(again, "wall_seconds")["expected"] == 10


def test_rerun_failed_is_not_gate_evidence(home, tmp_path):
    root = tmp_path / "work"
    card = _retry(root)
    full = bench.run(home, card)
    assert full["status"] == "failed"
    assert {item["name"]: item["status"] for item in full["cases"]} == {"good": "passed", "bad": "failed"}
    gate = bench.card_status(home, card, profile="all")
    assert gate["accepted"] is False
    assert gate["run_id"] == full["run_id"]

    (root / "cases" / "bad" / "marker").write_text("1\n")
    partial = bench.run(home, card, rerun_failed=full["run_id"])
    assert partial["status"] == "passed"
    assert [item["name"] for item in partial["cases"]] == ["bad"]
    gate = bench.card_status(home, card, profile="all")
    assert gate["run_id"] == full["run_id"]
    assert gate["accepted"] is False
    with pytest.raises(ForgeError, match="no failed"):
        bench.run(home, card, rerun_failed=partial["run_id"])


def test_data_list_and_prune(home, tmp_path):
    root = tmp_path / "work"
    card = _perf(root, worker=10)
    bench.run(home, card)
    data = home.root / "bench" / "data" / "perf"
    stale = data / "retired" / "alpha" / "measure"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("x")
    partial = data / "quick" / "alpha" / "measure" / "r000.partial"
    partial.mkdir()
    (partial / "junk.txt").write_text("y")
    listed = bench.data_list(home, "perf")
    assert any(row["step"] == "measure" and row["profile"] == "quick" for row in listed["rows"])
    with pytest.raises(ForgeError, match="--yes"):
        bench.data_prune(home, card)
    removed = bench.data_prune(home, card, yes=True)
    assert any(item.endswith("retired") or "/retired" in item for item in removed["removed"])
    assert not stale.exists()
    assert not partial.exists()
    assert (data / "quick" / "alpha" / "measure" / "r000" / "metrics.json").is_file()


def test_cli_compare_and_trend(home, tmp_path, capsys):
    root = tmp_path / "work"
    card = _perf(root, worker=10)
    first = bench.run(home, card)
    _perf(root, worker=11)
    second = bench.run(home, card)
    assert main(["--home", str(home.root), "bench", "compare", first["run_id"], second["run_id"]]) == 0
    text = capsys.readouterr().out
    assert "comparable" in text
    assert "wall_seconds" in text
    assert main(["--home", str(home.root), "--json", "bench", "trend", str(card),
                 "--case", "alpha", "--step", "speed", "--item", "wall_seconds"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["actual"] for row in payload["rows"]] == [10, 11]
