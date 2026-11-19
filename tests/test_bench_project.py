from pathlib import Path
import sys

import pytest
import yaml

from jarvis_forge import bench, bench_exec, bench_project
from jarvis_forge.storage import ForgeError, ForgeHome, write_json


def test_workspace_isolates_external_steps_and_pruning(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    config = tmp_path / "executor.yaml"
    config.write_text(yaml.safe_dump({"kind": "local", "data_root": str(tmp_path / "shared")}))
    bench_exec.add_executor(home, "shared", config)
    workspaces = [tmp_path / group / "same-name" for group in ("one", "two")]
    paths = []
    for workspace in workspaces:
        bench_project.initialize(workspace)
        case = workspace / "data/cases/simple"
        case.mkdir(parents=True)
        (case / "case.yaml").write_text("{}\n")
        card = workspace / "bin/bench.yaml"
        card.write_text(yaml.safe_dump({"kind": "bench", "name": "local",
                                        "cases": "data/cases/*", "steps": [{
                                            "name": "write", "reuse": True,
                                            "commands": [[sys.executable, "-c",
                                                          "open('result.txt', 'w').write('ok')"]],
                                            "outputs": {"data": "result.txt"},
                                        }]}))
        monkeypatch.chdir(workspace / "deps")
        assert bench_project.resolve_card() == card
        assert bench_project.resolve_card("bench.yaml") == card
        assert bench.run(home, card, executor="shared")["status"] == "reported"
        paths.append(bench.data_list(home)["rows"][0]["path"])

    assert paths[0] != paths[1]
    leftovers = [Path(path) / "r001.partial" for path in paths]
    for path in leftovers:
        path.mkdir()
    project = bench_project.discover()
    active = home.root / "runs/bench-active/result.json"
    write_json(active, {"run_id": "bench-active", "project_id": bench_project.identity(project),
                        "status": "running"})
    with pytest.raises(ForgeError, match="active run"):
        bench.data_prune(home, yes=True)
    active.unlink()
    assert bench.data_prune(home, yes=True)["removed"] == [str(leftovers[1])]
    assert leftovers[0].is_dir()
    assert (workspaces[1] / "data/cases/simple/case.yaml").is_file()


def test_workspace_entrypoint_ambiguity_and_escaped_root(tmp_path, monkeypatch):
    bench_project.initialize(tmp_path)
    path = tmp_path / bench_project.MANIFEST
    manifest = yaml.safe_load(path.read_text())
    manifest["project"]["entrypoints"] += ["bin/secondary/bench.yaml"]
    path.write_text(yaml.safe_dump(manifest))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ForgeError, match="ambiguous"):
        bench_project.resolve_card("bench.yaml")
    assert bench_project.resolve_card("./bin/bench.yaml") == Path("bin/bench.yaml")

    (tmp_path / "outputs").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ForgeError, match="must not traverse symlinks"):
        bench_project.discover()
