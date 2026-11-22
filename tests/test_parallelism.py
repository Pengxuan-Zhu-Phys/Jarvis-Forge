"""Native build budgets respect the process allocation rather than the host size."""

import json
from types import SimpleNamespace

import pytest
import yaml

from jarvis_forge import parallelism
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeHome


@pytest.mark.parametrize("version", [1, 2])
def test_cpu_budget_respects_affinity_and_ancestor_cgroup_quota(monkeypatch, version):
    monkeypatch.setattr(parallelism, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(parallelism.os, "sched_getaffinity", lambda _: set(range(20)), raising=False)
    files = {"/proc/self/cgroup": "0::/jobs/task" if version == 2 else "4:cpu,cpuacct:/jobs/task",
             "/proc/self/mountinfo": "1 2 0:1 /jobs /cg rw - cgroup2 cgroup rw" if version == 2
             else "1 2 0:1 /jobs /cg rw - cgroup cgroup rw,cpu,cpuacct"}
    if version == 2:
        files.update({"/cg/task/cpu.max": "max 100000", "/cg/cpu.max": "250000 100000"})
    else:
        files.update({"/cg/task/cpu.cfs_quota_us": "-1", "/cg/cpu.cfs_quota_us": "250000",
                      "/cg/cpu.cfs_period_us": "100000"})
    monkeypatch.setattr(parallelism, "_read", lambda path: files.get(str(path)))
    assert parallelism.available_cpus() == 3
    files["/cg/cpu.max" if version == 2 else "/cg/cpu.cfs_quota_us"] = "malformed"
    assert parallelism.available_cpus() == 20


def test_cpu_fallback_and_backend_caps(monkeypatch):
    monkeypatch.setattr(parallelism, "sys", SimpleNamespace(platform="darwin"))
    def unsupported(_):
        raise OSError("affinity unavailable")
    monkeypatch.setattr(parallelism.os, "sched_getaffinity", unsupported, raising=False)
    monkeypatch.setattr(parallelism.os, "process_cpu_count", lambda: None, raising=False)
    assert parallelism.available_cpus() == 1
    monkeypatch.setattr(parallelism.os, "process_cpu_count", lambda: 300)
    assert parallelism.policy() == {"available_cpus": 300, "build_jobs": 256,
                                   "concurrent_packages": 16, "cmake_jobs": 16,
                                   "homebrew_make_jobs": 16}


@pytest.mark.parametrize("args", [["apt", "install", "gsl"], ["build", "install", "sample"],
                                  ["build", "upgrade", "sample"], ["profile", "create", "ubuntu24"]])
@pytest.mark.parametrize("option", ["--jobs", "-j"])
def test_native_cli_rejects_manual_parallelism_without_creating_data(tmp_path, capsys, args, option):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "--json", *args, option, "8"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert option in result["error"]["message"]
    assert not home.exists()


def test_spack_config_uses_new_installer_and_global_tokens(tmp_path, monkeypatch):
    from jarvis_forge import spack_backend
    monkeypatch.setattr(parallelism, "available_cpus", lambda: 48)
    home = ForgeHome(tmp_path / "forge")
    spack_backend._config(home, tmp_path / "repository", {"prefix": "/python", "version": "3.13"})
    config = yaml.safe_load((home.root / "spack/config/config.yaml").read_text())["config"]
    assert {key: config[key] for key in ("installer", "build_jobs", "concurrent_packages")} == {
        "installer": "new", "build_jobs": 48, "concurrent_packages": 16}
