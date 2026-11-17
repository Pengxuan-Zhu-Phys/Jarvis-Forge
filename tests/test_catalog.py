import json

import pytest
import yaml

from jarvis_forge import checker, testing
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError


def project(tmp_path, names=("test_a.py", "test_b.py", "test_cli.py")):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "pkg.py").write_text("x = 1\n")
    for name in names:
        (root / "tests" / name).write_text("def test_ok():\n    pass\n")
    return root


def group(group_id, match=("tests/test_*.py",), **fields):
    return {"id": group_id, "match": list(match), "inputs": ["src/*.py"], **fields}


def card(root, groups=(), checks=(), name="jforge-tests.yaml", **fields):
    config = {"schema_version": 2, "kind": "checker", "id": "demo", "project": "..", **fields}
    if checks:
        config["checks"] = list(checks)
    if groups:
        config["groups"] = list(groups)
    path = root / "tests" / name
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def fast_slow(**extra):
    return [group("fast", markers="not slow", skip_allow=[]), group("slow", markers="slow", skip_allow=[]),
            group("cli", ["tests/test_cli.py"], markers="not slow"), *extra.get("more", [])]


def failure(args, capsys):
    assert main([*args, "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["ok"] is False
    return error["error"]["message"]


def test_profiles_default_named_and_group_selection_with_prerequisites(tmp_path):
    root = project(tmp_path)
    check = {"id": "lint", "argv": ["true"], "cache": False}
    groups = fast_slow()
    groups[1]["needs"] = ["lint"]
    path = card(root, groups, [check], profiles={"daily": ["fast"], "acceptance": ["fast", "slow"]}, default_profile="daily")
    loaded = testing.load_card(path)
    assert testing.select(loaded) == {"profile": "daily", "seeds": ["fast"], "ids": ["fast"]}
    assert testing.select(loaded, "acceptance")["ids"] == ["lint", "fast", "slow"]
    assert testing.select(loaded, groups=["slow"])["ids"] == ["lint", "slow"]
    with pytest.raises(ForgeError, match="no profile 'release'"):
        testing.select(loaded, "release")
    with pytest.raises(ForgeError, match="no group or check missing"):
        testing.select(loaded, groups=["missing"])


def test_cards_without_profiles_select_everything(tmp_path):
    root = project(tmp_path)
    path = card(root, [group("unit")], [{"id": "lint", "argv": ["true"], "cache": False}])
    assert testing.select(testing.load_card(path)) == {"profile": None, "seeds": ["lint", "unit"], "ids": ["lint", "unit"]}


def test_selection_option_conflicts_exit_2(tmp_path, capsys):
    root = project(tmp_path)
    path = card(root, fast_slow(), profiles={"daily": ["fast"]}, default_profile="daily")
    assert "mutually exclusive" in failure(["test", "plan", str(path), "--profile", "daily", "--group", "fast"], capsys)
    assert "catalogs only" in failure(["test", "plan", str(path), "--project", "x"], capsys)
    assert "catalogs only" in failure(["test", "validate", str(path), "--root", str(tmp_path)], capsys)


def test_markers_split_the_same_files_but_overlapping_groups_fail(tmp_path, capsys):
    root = project(tmp_path)
    path = card(root, fast_slow())
    plan = testing.plan(path, groups=["fast", "slow"])
    assert [step["files"] for step in plan["cards"][0]["steps"]] == [3, 3]
    assert "both select tests/test_cli.py" in failure(["test", "plan", str(path), "--group", "fast", "--group", "cli"], capsys)


def test_validate_checks_overlap_in_every_profile(tmp_path, capsys):
    root = project(tmp_path)
    path = card(root, fast_slow(), profiles={"daily": ["fast"], "mixed": ["fast", "cli"]}, default_profile="daily")
    assert main(["test", "plan", str(path)]) == 0
    capsys.readouterr()
    assert "in profile mixed" in failure(["test", "validate", str(path)], capsys)


def test_coverage_names_missing_files_and_rejects_stale_excludes(tmp_path):
    root = project(tmp_path)
    groups = [group("unit", ["tests/test_a.py"], skip_allow=[])]
    profiles = {"acceptance": ["unit"]}
    path = card(root, groups, profiles=profiles, default_profile="acceptance",
                coverage={"paths": ["tests/test_*.py"], "exclude": [{"path": "tests/test_b.py", "reason": "Needs a GPU."}]})
    with pytest.raises(ForgeError, match="outside every acceptance group: tests/test_cli.py"):
        testing.validate(path)
    path = card(root, groups, profiles=profiles, default_profile="acceptance",
                coverage={"paths": ["tests/test_*.py"], "exclude": [{"path": "tests/test_b.py", "reason": "GPU"},
                                                                     {"path": "tests/test_cli.py", "reason": "Manual"}]})
    assert testing.validate(path)["cards"][0]["coverage"] is True
    (root / "tests" / "test_b.py").unlink()
    with pytest.raises(ForgeError, match="missing file: tests/test_b.py"):
        testing.validate(path)
    with pytest.raises(ForgeError, match="needs a reason"):
        testing.load_card(card(root, groups, coverage={"paths": ["tests/*.py"], "exclude": [{"path": "tests/test_a.py", "reason": " "}]}))


def test_gate_profiles_require_skip_allow_and_coverage(tmp_path):
    root = project(tmp_path)
    path = card(root, [group("unit")], profiles={"acceptance": ["unit"]}, default_profile="acceptance")
    with pytest.raises(ForgeError, match="gate profile acceptance and must declare skip_allow"):
        testing.validate(path)
    path = card(root, [group("unit", skip_allow=["Redis"])], profiles={"release": ["unit"]}, default_profile="release")
    with pytest.raises(ForgeError, match="must declare coverage"):
        testing.validate(path)
    path = card(root, [group("unit", skip_allow=["Redis"])], profiles={"daily": ["unit"]}, default_profile="daily")
    assert testing.validate(path)["cards"][0]["warnings"] == []


def catalog(tmp_path, entries, root=".."):
    path = tmp_path / "books" / "ecosystem.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": 2, "kind": "test-catalog", "id": "eco", "root": root, "cards": entries}))
    return path


def test_catalog_contract_paths_and_root_override(tmp_path, capsys):
    root = project(tmp_path)
    gated = card(root, [group("unit", skip_allow=[])], profiles={"acceptance": ["unit"]}, default_profile="acceptance",
                 coverage={"paths": ["tests/test_*.py"]})
    path = catalog(tmp_path, [{"id": "demo", "path": "repo/tests/jforge-tests.yaml"},
                              {"id": "demo-min", "path": "repo/tests/jforge-tests.yaml", "env": "demo-min"}])
    result = testing.validate(path)
    assert [c["project_id"] for c in result["cards"]] == ["demo", "demo-min"]
    assert result["cards"][1]["env"] == "demo-min"
    plan = testing.plan(path, project="demo-min")
    assert [c["project_id"] for c in plan["cards"]] == ["demo-min"]
    assert "no card 'other'" in failure(["test", "plan", str(path), "--project", "other"], capsys)

    moved = catalog(tmp_path, [{"id": "demo", "path": "repo/tests/jforge-tests.yaml"}], root="nowhere")
    assert "root directory missing" in failure(["test", "validate", str(moved)], capsys)
    assert main(["test", "validate", str(moved), "--root", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    escape = catalog(tmp_path, [{"id": "demo", "path": "../outside.yaml"}], root="../repo")
    (tmp_path / "outside.yaml").write_text(gated.read_text())
    assert "leaves the catalog root" in failure(["test", "validate", str(escape)], capsys)

    card(root, [group("unit")], name="plain.yaml")
    plain = catalog(tmp_path, [{"id": "plain", "path": "repo/tests/plain.yaml"}])
    assert "Catalog card plain: Card demo has no acceptance profile" in failure(["test", "validate", str(plain)], capsys)


def test_empty_selected_group_fails_and_unselected_empty_group_warns(tmp_path, capsys):
    root = project(tmp_path)
    groups = [group("unit"), group("gpu", ["tests/gpu_*.py"]), group("thin", ["tests/test_a.py"], omit=["tests/test_a.py"])]
    path = card(root, groups, profiles={"daily": ["unit"], "thin": ["thin"]}, default_profile="daily")
    plan = testing.plan(path)
    assert plan["cards"][0]["warnings"] == ["Group gpu matches no files.", "Group thin matches no files."]
    assert "Selected group thin matches no files after omit" in failure(["test", "plan", str(path), "--profile", "thin"], capsys)
    assert testing.validate(path)["cards"][0]["warnings"] == ["Group gpu matches no files.", "Group thin matches no files."]


def test_adding_a_file_never_moves_existing_files_between_shards(tmp_path):
    root = project(tmp_path, [f"test_{n}.py" for n in range(40)])
    path = card(root, [group("unit")])

    def shards():
        return {k: testing.plan(path, batch=f"{k}/4")["cards"][0]["steps"][0] for k in range(1, 5)}

    before = shards()
    assert sorted(p for s in before.values() for p in s["paths"]) == sorted(f"tests/test_{n}.py" for n in range(40))
    (root / "tests" / "test_new.py").write_text("")
    after = shards()
    for k in range(1, 5):
        assert set(before[k]["paths"]) <= set(after[k]["paths"])
        assert after[k]["id"] == f"unit--{k}of4"
    assert len({s["shard_set_digest"] for s in after.values()}) == 1
    assert before[1]["shard_set_digest"] != after[1]["shard_set_digest"]


def test_an_empty_shard_is_reported_as_empty_batch(tmp_path, capsys):
    root = project(tmp_path, ["test_only.py"])
    path = card(root, [group("unit")])
    steps = [testing.plan(path, batch=f"{k}/3")["cards"][0]["steps"][0] for k in range(1, 4)]
    assert sorted(len(s["paths"]) for s in steps) == [0, 0, 1]
    empty = next(s for s in steps if not s["paths"])
    assert empty["reason"] == "empty batch"
    assert main(["test", "plan", str(path), "--batch", f"{empty['batch']['index']}/3"]) == 0
    assert "empty batch" in capsys.readouterr().out


@pytest.mark.parametrize("batch", ["0/4", "5/4", "1/10000", "2", "a/b"])
def test_batch_syntax(batch, tmp_path, capsys):
    root = project(tmp_path)
    assert "--batch takes k/n" in failure(["test", "plan", str(card(root, [group("unit")])), "--batch", batch], capsys)


@pytest.mark.parametrize("fields, message", [
    ({"id": "g" * 97}, "longer than 96"),
    ({"inputs": []}, "cached and must declare inputs"),
    ({"isolate": "installed"}, "must list distributions"),
    ({"isolate": "installed", "distributions": ["demo"], "pythonpath": ["src"]}, "cannot set pythonpath"),
    ({"pytest_args": ["--override-ini=addopts="]}, "does not allow"),
    ({"pytest_args": ["-q", "-vv"]}, "repeats -vv"),
    ({"env": {"HTS_NATIVE_TESTS": 1}}, "quote numbers"),
    ({"env": {"PYTHONPATH": "src"}}, "use the pythonpath field"),
    ({"env": {"JFORGE_STEP_DIR": "x"}}, "Forge sets it"),
    ({"skip_allow": ["("]}, "invalid pattern"),
    ({"workers": 0}, "workers must be auto"),
    ({"match": ["../other/test_x.py"]}, "without '..'"),
    ({"colour": "red"}, "Unknown test group fields: colour"),
])
def test_group_field_rules(fields, message, tmp_path):
    root = project(tmp_path)
    with pytest.raises(ForgeError, match=message):
        testing.load_card(card(root, [group("unit", **fields)]))


def test_group_defaults_and_pytest_argument_forms(tmp_path):
    root = project(tmp_path)
    path = card(root, [group("unit", pytest_args=["--tb", "line", "-p", "no:cacheprovider", "--maxfail=3", "-vv"],
                             env={"JFORGE_SPACK_INTEGRATION": "1"}, workers="auto"),
                       group("wheel", ["tests/test_a.py"], isolate="installed", distributions=["demo"], markers=" not slow ")])
    loaded = testing.load_card(path)
    unit, wheel = loaded["groups"]["unit"], loaded["groups"]["wheel"]
    assert unit["pytest_args"] == ["--tb=line", "-p", "no:cacheprovider", "--maxfail=3", "-vv"]
    assert (unit["isolate"], unit["import_mode"], unit["skip_allow"]) == ("source", "prepend", None)
    assert (wheel["import_mode"], wheel["markers"]) == ("importlib", "not slow")


def test_card_level_rules(tmp_path):
    root = project(tmp_path)
    cases = [
        ({"profiles": {"daily": ["unit"]}}, "must set default_profile"),
        ({"default_profile": "daily"}, "default_profile requires profiles"),
        ({"profiles": {"daily": ["nope"]}, "default_profile": "daily"}, "names unknown IDs: nope"),
        ({"profiles": {"daily": []}, "default_profile": "daily"}, "Profile daily must be a nonempty list"),
        ({"selection": "all"}, "Unknown test card fields: selection"),
    ]
    for fields, message in cases:
        with pytest.raises(ForgeError, match=message):
            testing.load_card(card(root, [group("unit")], **fields))
    with pytest.raises(ForgeError, match="Group and check IDs must be unique: unit"):
        testing.load_card(card(root, [group("unit")], [{"id": "unit", "argv": ["true"], "cache": False}]))
    with pytest.raises(ForgeError, match="needs unknown IDs: lint"):
        testing.load_card(card(root, [group("unit", needs=["lint"])]))
    with pytest.raises(ForgeError, match="Dependency cycle"):
        testing.load_card(card(root, [group("a", needs=["b"]), group("b", needs=["a"])]))
    with pytest.raises(ForgeError, match="at least one group or check"):
        testing.load_card(card(root))


def test_package_checks_and_exclusive_resources(tmp_path):
    root = project(tmp_path)
    smoke = {"id": "wheel-smoke", "exclusive": ["redis"],
             "check": {"package": {"distribution": "Jarvis-HEP", "modules": ["jarvishep2"], "commands": [["Jarvis", "-h"]]}}}
    loaded = testing.load_card(card(root, [group("unit", exclusive=["redis"])], [smoke]))
    assert testing.check_kind(loaded["checks"]["wheel-smoke"]) == "package"
    mixed = {"id": "x", "check": {"package": {"distribution": "demo"}, "python": {"version": ">=3.10"}}}
    with pytest.raises(ForgeError, match="cannot be combined"):
        testing.load_card(card(root, [], [mixed]))
    with pytest.raises(ForgeError, match="Invalid identifier"):
        testing.load_card(card(root, [group("unit", exclusive=["bad name"])]))


def test_plan_neither_imports_the_project_nor_creates_the_forge_home(tmp_path, capsys):
    root = project(tmp_path)
    (root / "tests" / "conftest.py").write_text("open(__file__ + '.imported', 'w').close()\n")
    home = tmp_path / "home"
    path = card(root, [group("unit")])
    assert main(["test", "plan", str(path), "--home", str(home), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert main(["test", "validate", str(path), "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "test validate"
    assert not home.exists()
    assert not (root / "tests" / "conftest.py.imported").exists()
    assert plan["ok"] is True and plan["command"] == "test plan"
    assert plan["cards"][0]["steps"][0]["paths"] == ["tests/test_a.py", "tests/test_b.py", "tests/test_cli.py"]


def test_schema_boundaries_between_checker_and_test(tmp_path, capsys):
    root = project(tmp_path)
    old = root / "tests" / "old.yaml"
    old.write_text(yaml.safe_dump({"schema_version": 1, "kind": "checker", "id": "old", "project": "..",
                                   "profiles": {"daily": ["x"]},
                                   "checks": [{"id": "x", "argv": ["true"], "cache": False}]}))
    assert "require schema_version: 2" in failure(["test", "validate", str(old)], capsys)
    new = card(root, [group("unit")])
    with pytest.raises(ForgeError, match="read by jforge test"):
        checker.load(new)
    assert main(["test", "validate", str(new), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "test validate"
    assert main(["test", "plan", str(new), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "test plan"
    assert main(["test", "list", str(new), "--json"]) == 2


@pytest.mark.parametrize("command", [["test"], ["test", "validate"], ["test", "plan"]])
def test_help(command, capsys):
    assert main([*command, "-h"]) == 0
    out = capsys.readouterr().out
    assert "Usage: jforge test" in out and "Show this message and exit." in out
    assert "\x1b[" not in out
