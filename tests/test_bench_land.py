"""Slurm execution, prefetch, coverage and uses: bench."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest
import yaml

from jarvis_forge import bench, bench_exec, testing_run
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json


FAKE_SLURM = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

root = Path(os.environ["JFORGE_FAKE_SLURM"])
state_path = root / "state.json"
name = Path(sys.argv[0]).name


def load():
    if state_path.is_file():
        return json.loads(state_path.read_text())
    return {"next": 1, "jobs": {}, "submits": 0}


def save(state):
    state_path.write_text(json.dumps(state))


def job_id(argv):
    if "-j" in argv:
        return argv[argv.index("-j") + 1]
    return argv[-1]


if name == "sbatch":
    script = Path(sys.argv[-1])
    text = script.read_text()
    indexes = [0]
    array = False
    for line in text.splitlines():
        if line.startswith("#SBATCH --array="):
            array = True
            indexes = []
            for part in line.split("=", 1)[1].split(","):
                if "-" in part:
                    start, end = part.split("-", 1)
                    indexes.extend(range(int(start), int(end) + 1))
                else:
                    indexes.append(int(part))
    state = load()
    job = str(state["next"])
    state["next"] += 1
    state["submits"] = state.get("submits", 0) + 1
    mode = os.environ.get("JFORGE_FAKE_SLURM_MODE", "inline")
    run_now = mode == "inline"
    slurm_state = "COMPLETED"
    queued = False
    if mode == "hold":
        run_now, slurm_state, queued = False, "PENDING", True
    elif mode == "timeout":
        run_now, slurm_state = False, "TIMEOUT"
    elif mode == "oom":
        run_now, slurm_state = False, "OUT_OF_MEMORY"
    elif mode == "node_fail":
        run_now, slurm_state = False, "NODE_FAIL"
    elif mode == "node_fail_once":
        if state["submits"] == 1:
            run_now, slurm_state = False, "NODE_FAIL"
        else:
            run_now, slurm_state = True, "COMPLETED"
    state["jobs"][job] = {"state": slurm_state, "queued": queued, "indexes": indexes, "array": array}
    save(state)
    if run_now:
        command = next(line[len("exec "):] for line in text.splitlines() if line.startswith("exec "))
        for index in indexes:
            env = os.environ.copy()
            env["SLURM_ARRAY_TASK_ID"] = str(index)
            rendered = command.replace("${SLURM_ARRAY_TASK_ID}", str(index))
            subprocess.call(shlex.split(rendered), env=env)
    print(job)
elif name == "squeue":
    state = load()
    info = state["jobs"].get(job_id(sys.argv[1:]))
    if info and info.get("queued"):
        print(job_id(sys.argv[1:]))
elif name == "sacct":
    state = load()
    job = job_id(sys.argv[1:])
    info = state["jobs"].get(job)
    if info:
        if info.get("array"):
            for index in info["indexes"]:
                print(f"{job}_{index}|{info['state']}")
        else:
            print(f"{job}|{info['state']}")
elif name == "scancel":
    state = load()
    job = sys.argv[-1]
    info = state["jobs"].get(job)
    if info:
        info["queued"] = False
        info["state"] = "CANCELLED"
        save(state)
'''


@pytest.fixture
def home(tmp_path):
    value = ForgeHome(tmp_path / "home")
    value.initialize()
    return value


@pytest.fixture
def slurm(tmp_path, monkeypatch):
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    script = bindir / "slurm.py"
    script.write_text(FAKE_SLURM)
    script.chmod(0o755)
    for command in ("sbatch", "squeue", "scancel", "sacct"):
        (bindir / command).symlink_to(script)
    state = tmp_path / "slurm-state"
    state.mkdir()
    src = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("JFORGE_FAKE_SLURM", str(state))
    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "inline")
    monkeypatch.setenv("JFORGE_BENCH_POLL", "0")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return state


def _dump(path, payload):
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _card(root, *, repeats=1, retries=1, labels=("demo",), reuse=False, extra=None):
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "bench",
        "name": "demo",
        "default_profile": "smoke",
        "profiles": {"smoke": {"cases": ["*"]}},
        "cases": {"matrix": {"label": list(labels)}, "name": "${label}"},
        "steps": [{
            "name": "work",
            "repeat": repeats,
            "retries": retries,
            "reuse": reuse,
            "resources": {"cores": 2, "memory": "1G", "time": "1h"},
            "commands": [[sys.executable, "-c", "from pathlib import Path; Path('out.txt').write_text('ok')"]],
            "outputs": {"out": "out.txt"},
        }],
    }
    if extra:
        payload.update(extra)
    path = root / "bench.yaml"
    _dump(path, payload)
    return path


def _executor(home, tmp_path, *, queued=10, array_limit=1000):
    data = tmp_path / "scratch"
    data.mkdir(exist_ok=True)
    source = tmp_path / "site.yaml"
    _dump(source, {
        "kind": "slurm",
        "partition": "skylake",
        "account": "a1234",
        "max_queued": queued,
        "array_limit": array_limit,
        "data_root": str(data),
        "preamble": "echo site-preamble",
        "machine_class": "phoenix-test",
    })
    bench_exec.add_executor(home, "hpc", source)
    return data


def _jobs(run_dir):
    return read_json(run_dir / "slurm.json")["jobs"]


def _scripts(run_dir):
    return sorted((run_dir / "slurm").glob("*.sh"))


# Prefetch and coverage


def test_prefetch_is_required_and_its_digest_is_an_input(home, tmp_path):
    payload = tmp_path / "note.txt"
    payload.write_text("hello-fetch\n")
    archive = tmp_path / "sample.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(payload, arcname="note.txt")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    root = tmp_path / "card"
    root.mkdir()
    card = root / "bench.yaml"
    body = {
        "kind": "bench", "name": "fetched", "default_profile": "smoke",
        "profiles": {"smoke": {"cases": ["*"]}},
        "cases": {"matrix": {"label": ["demo"]}, "name": "${label}"},
        "fetch": [{"id": "sample", "url": archive.as_uri(), "sha256": digest, "unpack": True}],
        "steps": [{
            "name": "read", "reuse": True,
            "env": {"SAMPLE": "${fetch.sample.path}/note.txt"},
            "commands": [[sys.executable, "-c",
                          "import os; from pathlib import Path; Path('out.txt').write_text(Path(os.environ['SAMPLE']).read_text())"]],
            "outputs": {"out": "out.txt"},
        }],
    }
    _dump(card, body)
    with pytest.raises(ForgeError, match="not in the cache") as caught:
        bench.plan(home, card)
    assert "prefetch" in caught.value.hint
    first = bench_exec.prefetch(home, card)
    assert Path(first["fetched"][0]["path"], "note.txt").read_text() == "hello-fetch\n"
    again = bench_exec.prefetch(home, card)
    assert again["fetched"][0]["path"] == first["fetched"][0]["path"]
    result = bench.run(home, card)
    assert result["status"] == "reported"
    slot = home.root / "bench" / "data" / "fetched" / "smoke" / "demo" / "read" / "r000"
    assert (slot / "out.txt").read_text() == "hello-fetch\n"
    done = read_json(slot / ".jforge" / "done.json")
    assert done["inputs"]["fetch"]["sample"] == digest

    payload.write_text("changed\n")
    with tarfile.open(archive, "w:gz") as package:
        package.add(payload, arcname="note.txt")
    body["fetch"][0]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    _dump(card, body)
    bench_exec.prefetch(home, card)
    with pytest.raises(ForgeError, match="fetch"):
        bench.run(home, card)


def test_coverage_gaps_fail_and_a_gate_requires_the_command(home, tmp_path):
    root = tmp_path / "card"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    listed = scripts / "list_cases.py"
    listed.write_text("#!/usr/bin/env python3\nprint('demo')\nprint('other')\n")
    listed.chmod(0o755)
    card = _card(root, extra={"coverage": {"list": ["scripts/list_cases.py"], "exclude": {"other": "held out"}}})
    loaded = bench.validate(card)
    assert loaded["coverage"] == "checked"
    listed.write_text("#!/usr/bin/env python3\nprint('missing-case')\n")
    with pytest.raises(ForgeError, match="Coverage gap"):
        bench.validate(card)
    gap = root / "gap.yaml"
    _dump(gap, {
        "kind": "bench", "name": "gap", "cases": {"matrix": {"label": ["demo"]}, "name": "${label}"},
        "steps": [{"name": "work", "commands": ["python3 -c \"open('out.txt','w').write('ok')\""], "outputs": {"out": "out.txt"}}],
        "profiles": {"gate": {"cases": ["*"], "require": "passed"}},
        "coverage": {"list": ["scripts/missing-tool"]},
    })
    assert bench.validate(gap)["coverage"] == "not checked"
    with pytest.raises(ForgeError, match="Coverage command"):
        bench.run(home, gap, profile="gate")


# Slurm


def test_slurm_submits_one_array_and_keeps_the_site_settings(home, tmp_path, slurm):
    data = _executor(home, tmp_path, array_limit=2)
    card = _card(tmp_path / "work", repeats=3)
    result = bench.run(home, card, executor="hpc")
    assert result["status"] == "reported"
    report = read_json(Path(result["path"]) / "report.json")
    assert report["machine_class"] == "phoenix-test"
    scripts = [path.read_text() for path in _scripts(Path(result["path"]))]
    assert any("--array=0,1" in text for text in scripts)
    assert any("--array=2" in text for text in scripts)
    assert all("--partition=skylake" in text and "--cpus-per-task=2" in text for text in scripts)
    assert all("--mem=1G" in text and "--time=01:00:00" in text and "echo site-preamble" in text for text in scripts)
    assert all("bench exec-task" in text for text in scripts)
    for index in range(3):
        assert (data / "demo" / "smoke" / "demo" / "work" / f"r{index:03d}" / "out.txt").read_text() == "ok"
    assert not (home.root / "bench" / "data" / "demo").exists()


def test_detach_stays_running_until_status_and_watch(home, tmp_path, slurm):
    _executor(home, tmp_path)
    card = _card(tmp_path / "work")
    detached = bench.run(home, card, executor="hpc", detach=True)
    assert detached["detached"] is True and detached["status"] == "running"
    assert read_json(Path(detached["path"]) / "result.json")["status"] == "running"
    watched = bench_exec.watch(home, detached["run_id"])
    assert watched["status"] == "reported"
    code = main(["--home", str(home.root), "bench", "status", detached["run_id"]])
    assert code == 0


def test_max_queued_submits_one_wave_and_cancel_scancels(home, tmp_path, slurm, monkeypatch):
    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "hold")
    _executor(home, tmp_path, queued=1)
    card = _card(tmp_path / "work", labels=("one", "two"))
    detached = bench.run(home, card, executor="hpc", detach=True)
    jobs = _jobs(Path(detached["path"]))
    assert len(jobs) == 1
    assert jobs[0]["case"] in {"one", "two"}
    with pytest.raises(ForgeError, match="--yes"):
        bench_exec.cancel(home, detached["run_id"])
    cancelled = bench_exec.cancel(home, detached["run_id"], yes=True)
    assert cancelled["status"] == "cancelled"
    state = json.loads((slurm / "state.json").read_text())
    assert state["jobs"][jobs[0]["id"]]["state"] == "CANCELLED"
    status = bench.card_status(home, card)
    assert status["accepted"] is False
    code = main(["--home", str(home.root), "executor", "show", "hpc"])
    assert code == 0


def test_lost_jobs_are_resubmitted_and_resource_failures_are_not(home, tmp_path, slurm, monkeypatch):
    _executor(home, tmp_path)
    card = _card(tmp_path / "work")
    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "node_fail_once")
    recovered = bench.run(home, card, executor="hpc")
    assert recovered["status"] == "reported"
    assert len(_jobs(Path(recovered["path"]))) == 2

    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "node_fail")
    lost = bench.run(home, card, executor="hpc")
    assert lost["status"] == "error"
    assert len(_jobs(Path(lost["path"]))) == 2
    failed = read_json(Path(lost["path"]) / "tasks" / "demo" / "work.0" / "state.json")
    assert failed["error"] == "lost after retries"

    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "timeout")
    timed = bench.run(home, card, executor="hpc")
    assert timed["status"] == "error"
    assert len(_jobs(Path(timed["path"]))) == 1
    reason = read_json(Path(timed["path"]) / "tasks" / "demo" / "work.0" / "state.json")["error"]
    assert "TIMEOUT" in reason and "resources" in reason

    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "oom")
    exhausted = bench.run(home, card, executor="hpc")
    assert len(_jobs(Path(exhausted["path"]))) == 1
    assert "OUT_OF_MEMORY" in read_json(Path(exhausted["path"]) / "tasks" / "demo" / "work.0" / "state.json")["error"]


def test_resume_retries_a_lost_detached_job(home, tmp_path, slurm, monkeypatch):
    _executor(home, tmp_path)
    card = _card(tmp_path / "work")
    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "node_fail")
    detached = bench.run(home, card, executor="hpc", detach=True)
    assert detached["status"] == "running"
    monkeypatch.setenv("JFORGE_FAKE_SLURM_MODE", "inline")
    resumed = bench.resume(home, detached["run_id"])
    assert resumed["status"] == "reported"
    assert len(_jobs(Path(resumed["path"]))) == 2


def test_executor_file_requires_a_partition(home, tmp_path):
    source = tmp_path / "site.yaml"
    source.write_text("kind: slurm\ndata_root: /scratch\nmachine_class: test\n")
    with pytest.raises(ForgeError, match="partition"):
        bench_exec.add_executor(home, "hpc", source)


# uses: bench


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=root,
                   check=True, capture_output=True)


def _shell(tmp_path):
    bindir = tmp_path / "pybin"
    bindir.mkdir()
    wrapper = bindir / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    wrapper.chmod(0o755)
    shell = tmp_path / "env.sh"
    shell.write_text(f'export PATH="{bindir}:$PATH"\n')
    return shell


def test_uses_bench_follows_the_recorded_run_and_the_subject(home, tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "lib.py").write_text("VALUE = 1\n")
    _dump(root / "bench.yaml", {
        "kind": "bench", "name": "demo", "subject": "code",
        "default_profile": "smoke", "profiles": {"smoke": {"cases": ["*"]}},
        "software": {"code": {"source": "src"}},
        "cases": {"matrix": {"label": ["demo"]}, "name": "${label}"},
        "steps": [{
            "name": "work", "software": ["code"], "reuse": False,
            "commands": [[sys.executable, "-c", "from pathlib import Path; Path('out.txt').write_text('ok')"]],
            "outputs": {"out": "out.txt"},
        }],
    })
    test_card = root / "tests" / "jforge-tests.yaml"
    _dump(test_card, {
        "schema_version": 2, "kind": "checker", "id": "demo", "project": "..",
        "environment": {"shell": str(_shell(tmp_path))},
        "checks": [{"id": "sample", "uses": {"bench": "../bench.yaml", "profile": "smoke"}}],
        "profiles": {"acceptance": ["sample"]}, "default_profile": "acceptance",
    })
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", "--allow-empty")
    with pytest.raises(ForgeError, match="relative"):
        from jarvis_forge.checker import validate_checks
        validate_checks([{"id": "bad", "uses": {"bench": "/tmp/bench.yaml", "profile": "smoke"}}])

    early = testing_run.run(home, test_card, profile="acceptance")
    assert early["status"] == "failed"
    assert "No bench run covers" in early["runs"][0]["steps"][0]["error"]

    bench.run(home, root / "bench.yaml")
    accepted = testing_run.run(home, test_card, profile="acceptance")
    assert accepted["status"] == "passed"
    assert accepted["summary"]["accepted"] is True
    assert main(["--home", str(home.root), "test", "status", str(test_card), "--profile", "acceptance"]) == 0

    (root / "src" / "lib.py").write_text("VALUE = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "edit")
    changed = testing_run.run(home, test_card, profile="acceptance")
    assert changed["status"] == "failed"
    assert "software changed" in changed["runs"][0]["steps"][0]["error"]
    assert main(["--home", str(home.root), "test", "status", str(test_card), "--profile", "acceptance"]) == 1

    bench.run(home, root / "bench.yaml")
    (root / "src" / "lib.py").write_text("VALUE = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "restore")
    restored = testing_run.run(home, test_card, profile="acceptance")
    assert restored["status"] == "failed"
    assert "software changed" in restored["runs"][0]["steps"][0]["error"]
