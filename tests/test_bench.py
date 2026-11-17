"""Local bench runs: reuse, blocking, compare and the CLI."""

import json
from pathlib import Path
import sys

import pytest
import yaml

from jarvis_forge import bench
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json


@pytest.fixture
def home(tmp_path):
    value = ForgeHome(tmp_path / "home")
    value.initialize()
    return value


def _write(root, *, chunks=2, events=10, analyse_reuse=False):
    generator = root / "src" / "generator"
    cbs = root / "src" / "cbs"
    generator.mkdir(parents=True, exist_ok=True)
    cbs.mkdir(parents=True, exist_ok=True)
    (generator / "version.txt").write_text("1\n")
    (cbs / "version.txt").write_text("1\n")
    (root / "launch.txt").write_text("events=${settings.events}\nseed=${seed}\n")
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "check_events.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "text = Path(sys.argv[1]).read_text()\n"
        "raise SystemExit(0 if 'events=' in text else 1)\n")
    (scripts / "analyse.py").write_text(
        "import json\njson.dump({'regions': {'SR': {'yield': 1.0, 'err': 0.1}}}, open('result.json', 'w'))\n")
    case = root / "cases" / "demo"
    (case / "generate").mkdir(parents=True, exist_ok=True)
    (case / "generate" / "proc_card.dat").write_text("generate p p > t t~\n")
    (case / "cbs.yaml").write_text("analysis: DEMO\n")
    (case / "reference.yaml").write_text(
        "source: test\ntolerance: {sigma: 2}\nvalues:\n  SR: {value: 1.0, error: 0.1}\n")
    (case / "case.yaml").write_text("analysis: DEMO\ntags: [validated]\nsettings: {events: 50}\n")
    python = json.dumps(sys.executable)
    verify = json.dumps(f"{sys.executable} ${{bench.dir}}/scripts/check_events.py events.txt")
    card = root / "bench.yaml"
    card.write_text(f"""kind: bench
name: demo
subject: cbs
software:
  generator: {{source: src/generator}}
  cbs: {{source: src/cbs}}
settings: {{events: 10, chunks: 2}}
cases: cases/*
steps:
  - name: generate
    software: [generator]
    repeat: ${{settings.chunks}}
    seed: 1000
    reuse: true
    clean: true
    files:
      - ${{case.dir}}/generate/
    templates:
      - ${{bench.dir}}/launch.txt
    commands:
      - [{python}, "-c", "from pathlib import Path; Path('events.txt').write_text(Path('launch.txt').read_text())"]
    outputs: {{events: events.txt}}
    verify: {verify}
  - name: analyse
    after: [generate]
    software: [cbs]
    reuse: {str(analyse_reuse).lower()}
    files:
      - ${{case.dir}}/cbs.yaml
      - ${{bench.dir}}/scripts/analyse.py
    commands:
      - [{python}, analyse.py, "${{generate.outputs.events}}"]
    outputs: {{result: result.json}}
  - name: check
    after: [analyse]
    compare:
      actual: ${{analyse.outputs.result}}
      select: regions.*
      value: yield
      error: err
      expected: ${{case.dir}}/reference.yaml
      tolerance: {{sigma: 2}}
default_profile: smoke
profiles:
  smoke: {{cases: [demo], settings: {{events: {events}, chunks: {chunks}}}}}
  full: {{tags: [validated], settings: {{events: {events}, chunks: {chunks}}}}}
  acceptance: {{tags: [validated], require: complete, data: smoke, settings: {{events: {events}, chunks: {chunks}}}}}
""")
    return card


def _events(home, profile, index):
    return home.root / "bench" / "data" / "demo" / profile / "demo" / "generate" / f"r{index:03d}" / "events.txt"


def _done(home, profile, step, index):
    path = home.root / "bench" / "data" / "demo" / profile / "demo" / step / f"r{index:03d}" / ".jforge" / "done.json"
    return read_json(path)


def test_judge_applies_every_tolerance():
    assert bench.judge(1.0, 1.2, 0.1, 0.1, {"sigma": 2})["status"] == "passed"
    assert bench.judge(5.0, 1.0, 0.1, 0.1, {"sigma": 2})["status"] == "failed"
    assert bench.judge(1.0, 1.0, None, None, None)["status"] == "reported"
    assert bench.judge(3.0, 2.0, None, None, {"abs": 1, "rel": 0.6})["status"] == "passed"
    assert bench.judge(3.0, 2.0, None, None, {"abs": 1, "rel": 0.1})["status"] == "failed"
    assert bench.judge(1.2, 1.0, None, None, {"max_ratio": 1.15})["status"] == "failed"
    assert bench.judge(1.1, 1.0, None, None, {"min_ratio": 1.05})["status"] == "passed"
    assert bench.select_items({"regions": {"SR": {"yield": 1}}}, "regions.*") == {"SR": {"yield": 1}}


def test_validate_rejects_a_reserved_step_and_reads_a_matrix(tmp_path):
    card = tmp_path / "bench.yaml"
    card.write_text("kind: bench\nname: demo\ncases: {matrix: {sampler: [A]}, name: '${sampler}'}\nsteps: [{name: bench}]\n")
    with pytest.raises(ForgeError, match="reserved"):
        bench.validate(card)
    card.write_text("""kind: bench
name: demo
settings: {seeds: 1}
cases:
  matrix: {sampler: [Dynesty, MCMC], dim: [2, 5]}
  name: "${sampler}-${dim}d"
steps:
  - name: scan
    repeat: ${settings.seeds}
    commands: ["python3 -c \\"open('out.txt','w').write('ok')\\""]
    outputs: {result: out.txt}
""")
    loaded = bench.validate(card)
    assert loaded["cases"] == ["Dynesty-2d", "Dynesty-5d", "MCMC-2d", "MCMC-5d"]


def test_profile_settings_win_and_generate_is_reused(home, tmp_path):
    card = _write(tmp_path / "work")
    first = bench.run(home, card)
    assert first["status"] == "passed"
    assert _events(home, "smoke", 0).read_text() == "events=10\nseed=1000\n"
    assert _events(home, "smoke", 1).read_text() == "events=10\nseed=1001\n"
    assert not (_events(home, "smoke", 0).parent / "proc_card.dat").exists()
    generate = _done(home, "smoke", "generate", 0)["run"]
    analyse = _done(home, "smoke", "analyse", 0)["run"]
    second = bench.run(home, card)
    assert second["status"] == "passed"
    assert _done(home, "smoke", "generate", 0)["run"] == generate
    assert _done(home, "smoke", "analyse", 0)["run"] != analyse
    planned = bench.plan(home, card)
    assert "2/2 done" in planned["text"]
    assert "reuse" in planned["text"]


def test_section_45_changes(home, tmp_path):
    root = tmp_path / "work"
    card = _write(root)
    bench.run(home, card)
    original = _events(home, "smoke", 0).read_text()

    _write(root, chunks=4)
    bench.run(home, card)
    assert _events(home, "smoke", 0).read_text() == original
    assert _events(home, "smoke", 3).read_text() == "events=10\nseed=1003\n"
    planned = bench.plan(home, card)
    generate = next(step for step in planned["cases"][0]["steps"] if step["name"] == "generate")
    assert generate["action"] == "reuse"
    assert "4/4" in generate["detail"]

    slot = home.root / "bench/data/demo/smoke/demo/generate/r001"
    # Keep the finished copy out of the way and delete the slot the next run must fill.
    removed = _events(home, "smoke", 1).read_text()
    import shutil
    shutil.rmtree(slot)
    bench.run(home, card)
    assert _events(home, "smoke", 0).read_text() == original
    assert _events(home, "smoke", 1).read_text() == removed

    _write(root, chunks=4, events=20)
    with pytest.raises(ForgeError, match="inputs differ") as caught:
        bench.run(home, card)
    message = str(caught.value)
    assert "--redo generate" in message and "--keep generate" in message
    assert "events" in message
    kept = bench.run(home, card, keep=["generate"])
    assert kept["status"] == "passed"
    assert _events(home, "smoke", 0).read_text() == original
    assert _done(home, "smoke", "generate", 0)["kept"]

    redone = bench.run(home, card, redo=["generate"])
    assert redone["status"] == "passed"
    assert _events(home, "smoke", 0).read_text() == "events=20\nseed=1000\n"
    assert _events(home, "smoke", 3).read_text().startswith("events=20\n")


def test_only_the_step_that_uses_changed_software_is_blocked(home, tmp_path):
    root = tmp_path / "work"
    card = _write(root, analyse_reuse=True)
    bench.run(home, card)
    (root / "src" / "cbs" / "version.txt").write_text("2\n")
    with pytest.raises(ForgeError, match="analyse: inputs differ"):
        bench.run(home, card)
    assert _done(home, "smoke", "generate", 0)["inputs"]["software"]["generator"].startswith("source:")
    (root / "src" / "cbs" / "version.txt").write_text("1\n")
    (root / "src" / "generator" / "version.txt").write_text("2\n")
    with pytest.raises(ForgeError, match="generate: inputs differ"):
        bench.plan(home, card)


def test_reference_and_case_files_do_not_rebuild_generate(home, tmp_path):
    root = tmp_path / "work"
    card = _write(root)
    bench.run(home, card)
    generate = _done(home, "smoke", "generate", 0)["run"]
    (root / "cases" / "demo" / "cbs.yaml").write_text("analysis: OTHER\n")
    (root / "cases" / "demo" / "reference.yaml").write_text(
        "source: test\ntolerance: {sigma: 2}\nvalues:\n  SR: {value: 1.0, error: 0.1}\n")
    result = bench.run(home, card)
    assert result["status"] == "passed"
    assert _done(home, "smoke", "generate", 0)["run"] == generate


def test_verify_failure_makes_the_repeat_run_again(home, tmp_path):
    root = tmp_path / "work"
    card = _write(root)
    bench.run(home, card)
    first = _done(home, "smoke", "generate", 0)["run"]
    (root / "scripts" / "check_events.py").write_text("import sys\nraise SystemExit(1)\n")
    checked = bench.verify(home, card, step="generate")
    assert checked["failed"] == 2
    (root / "scripts" / "check_events.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "text = Path(sys.argv[1]).read_text()\n"
        "raise SystemExit(0 if 'events=' in text else 1)\n")
    bench.run(home, card)
    assert _done(home, "smoke", "generate", 0)["run"] != first


def test_failed_comparison_exits_as_a_result(home, tmp_path):
    root = tmp_path / "work"
    card = _write(root)
    bench.run(home, card)
    (root / "cases" / "demo" / "reference.yaml").write_text(
        "source: test\ntolerance: {sigma: 2}\nvalues:\n  SR: {value: 100.0, error: 0.1}\n")
    result = bench.run(home, card)
    assert result["status"] == "failed"
    assert result["cases"][0]["status"] == "failed"
    code = main(["--home", str(home.root), "bench", "status", str(card), "--profile", "smoke"])
    assert code == 1


def test_acceptance_profile_reuses_another_profiles_data(home, tmp_path):
    card = _write(tmp_path / "work")
    bench.run(home, card, profile="smoke")
    generate = _done(home, "smoke", "generate", 0)["run"]
    result = bench.run(home, card, profile="acceptance")
    assert result["status"] == "reported" or result["status"] == "passed"
    assert _done(home, "smoke", "generate", 0)["run"] == generate
    code = main(["--home", str(home.root), "--json", "bench", "status", str(card), "--profile", "acceptance"])
    assert code == 0


def test_cli_plan_and_history(home, tmp_path, capsys):
    card = _write(tmp_path / "work")
    assert main(["--home", str(home.root), "bench", "run", str(card)]) == 0
    capsys.readouterr()
    assert main(["--home", str(home.root), "--json", "bench", "history", "--card", "demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"][0]["name"] == "demo"
    assert payload["runs"][0]["status"] == "passed"
