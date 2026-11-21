"""Spack selection and passive legacy history, without a legacy backend."""

import json
from pathlib import Path
import shutil
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from jarvis_forge import apt_profiles, homebrew_backend, spack_backend as b
from jarvis_forge.apt_profiles import active_home, archived_packages, profiles, switch
from jarvis_forge.cli import main
from jarvis_forge.shell_activation import activation
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


@pytest.fixture
def base(tmp_path, monkeypatch, linux_apt):
    base = ForgeHome(tmp_path / "user home")
    base.initialize()

    def bootstrap(home, python=None, progress=None):
        home.initialize()
        write_json(
            home.root / "spack/backend.json", {"schema_version": 1, "backend": "spack"}
        )
        file = home.root / "environments/apt/revisions/empty/activate.sh"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(activation(home, []))
        write_json(
            home.root / "packages.json",
            {
                "schema_version": 1,
                "backend": "spack",
                "active": {},
                "manual": [],
                "activation": str(file),
                "last_operation_id": "empty",
                "spack": {"roots": [], "packages": [], "environment": None},
            },
        )
        return {"backend": "spack", "state_committed": True}

    monkeypatch.setattr(b, "bootstrap", bootstrap)
    return base




















def test_shell_hook_is_available_with_invalid_preferences(base, tmp_path, monkeypatch, capsys):
    write_json(base.root / "profiles/selection.json", {"invalid":True})
    config = tmp_path / "config"
    preference = config / "jarvis-forge/default-home.json"
    preference.parent.mkdir(parents=True)
    preference.write_text("{broken")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("JFORGE_HOME", raising=False)
    for options in [[], ["--home", str(base.root)]]:
        assert main([*options, "shell-init"]) == 0
        assert "jforge() {" in capsys.readouterr().out


def test_shell_hook_initial_activation_uses_explicit_home(tmp_path, capsys):
    home = tmp_path / "explicit home"
    assert main(["--home", str(home), "shell-init"]) == 0
    hook = capsys.readouterr().out
    fake = tmp_path / "jforge"
    fake.write_text('#!/bin/bash\n[ "$1" = --home ] && [ "$2" = "$EXPECTED_HOME" ] && printf "%s\\n" "$ACTIVATION"\n')
    fake.chmod(0o700)
    activation = tmp_path / "activate.sh"
    activation.write_text('export FIXTURE_HOME=selected\n')
    result = subprocess.run(["bash", "-c", 'export PATH="$1:$PATH" EXPECTED_HOME="$2" ACTIVATION="$3"; eval "$4"; printf "%s" "$FIXTURE_HOME"',
                             "fixture", str(tmp_path), str(home), str(activation), hook], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == "selected"


@pytest.mark.parametrize(
    "arguments,expected,activation_status",
    [
        ("apt search install", False, 0),
        ("apt install toy --dry-run", False, 0),
        ("apt switch --help", False, 0),
        ("apt switch", False, 0),
        ("apt switch spack --name gambit", True, 0),
        ("apt profile", False, 0),
        ("apt profile --archived", False, 0),
        ("apt profile gambit", True, 0),
        ("apt profile gambit --create", True, 0),
        ("apt import spack", True, 0),
        ("apt install toy", True, 0),
        ("apt install toy", True, 7),
        ("--json apt install toy", False, 7),
        ("apt install toy --json", False, 7),
    ],
)
def test_shell_hook_only_refreshes_after_changes(tmp_path, capsys, arguments, expected, activation_status):
    assert main(["--home", str(tmp_path / "home"), "shell-init"]) == 0
    hook = capsys.readouterr().out
    fake = tmp_path / "jforge"
    fake.write_text(
        '#!/bin/bash\n'
        'if [[ " $* " == *" --json "* ]]; then printf \'{"ok":true}\\n\'; fi\n'
        'if [ "${_JFORGE_SHELL_HANDOFF-}:$HANDOFF" = 1:1 ]; then printf "source\\0%s\\0" "$ACTIVATION" >&9; fi\n'
    )
    fake.chmod(0o755)
    file = tmp_path / "activate.sh"
    file.write_text(f'printf "activated\\n"\nreturn {activation_status}\n')
    result = subprocess.run(
        [
            "bash",
            "--noprofile", "--norc", "-ic",
            'export PATH="$1:$PATH" ACTIVATION="$2" HANDOFF="$4"; eval "$3" >/dev/null 2>&1; jforge '
            + arguments,
            "test",
            str(tmp_path),
            str(file),
            hook,
            str(int(expected)),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ("activated" in result.stderr) == expected
    assert ("Could not activate" in result.stderr) == (expected and activation_status != 0)
    assert result.stdout == ('{"ok":true}\n' if "--json" in arguments else "")


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_hook_activates_only_successful_interactive_builds(tmp_path, capsys, shell):
    if not shutil.which(shell):
        pytest.skip("shell is not installed")
    assert main(["shell-init"]) == 0
    hook = capsys.readouterr().out
    fake = tmp_path / "jforge"
    fake.write_text(
        '#!/bin/bash\n'
        'case "$1 ${2-}" in\n'
        '  "build install") ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
        'if [ "${_JFORGE_SHELL_HANDOFF-}" = 1 ] && [ "$3" != empty ]; then\n'
        '    case " $* " in *" --json "*|*" --no-activate "*) ;; *) printf "build\\0%s\\0" "$ACTIVATION" >&9 ;; esac\n'
        'fi\n'
        '[ "$3" != failed ] || exit 7\n'
    )
    fake.chmod(0o700)
    script = tmp_path / "some workspace's activation.sh"
    script.write_text('export FIXTURE_BUILD=loaded\n')
    code = r'''
export PATH="$1:$PATH" ACTIVATION="$2" TMPDIR="$1"
eval "$3"
jforge build install fixture
printf 'run %s\n' "${FIXTURE_BUILD-unset}"
unset FIXTURE_BUILD
jforge build install fixture
printf 'resume %s\n' "${FIXTURE_BUILD-unset}"
unset FIXTURE_BUILD
jforge build install failed
printf 'failed %s %s\n' "$?" "${FIXTURE_BUILD-unset}"
jforge build install empty
printf 'empty %s\n' "${FIXTURE_BUILD-unset}"
jforge build install fixture --json
printf 'json %s\n' "${FIXTURE_BUILD-unset}"
jforge build install fixture --no-activate
printf 'optout %s\n' "${FIXTURE_BUILD-unset}"
'''
    args = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
    result = subprocess.run([shell, *args, "-ic", code, "test", str(tmp_path), str(script), hook],
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["run loaded", "resume loaded", "failed 7 unset",
                                          "empty unset", "json unset", "optout unset"]
    assert result.stderr.count("Build environment activated.") == 2
    assert not list(tmp_path.glob("jforge-shell.*"))

    script.write_text("return 9\n")
    check = 'export PATH="$1:$PATH" ACTIVATION="$2"; eval "$3"; jforge build install fixture; echo $?'
    result = subprocess.run([shell, *args, "-ic", check, "test", str(tmp_path), str(script), hook],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "0" and "Could not activate" in result.stderr
    result = subprocess.run([shell, *args, "-c", check, "test", str(tmp_path), str(script), hook],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "0" and "Could not activate" not in result.stderr


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_managed_repair_refreshes_the_loaded_hook(tmp_path, capsys, shell):
    if not shutil.which(shell):
        pytest.skip("shell is not installed")
    assert main(["shell-init"]) == 0
    hook = capsys.readouterr().out
    fake = tmp_path / "jforge"
    fake.write_text(
        '#!/bin/sh\n[ "$1" != --home ] || shift 2\ncase "$1 ${2-}" in\n'
        '  "shell-init ") printf "%s\\n" "export FIXTURE_HOOK=refreshed" ;;\n'
        '  "env repair") [ "$#" != 2 ] || printf "hook\\0%s\\0" "$FIXTURE_HOME" >&9 ;;\n'
        '  *) exit 2 ;;\nesac\n'
    )
    fake.chmod(0o700)
    args = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
    code = ('export PATH="$1:$PATH" FIXTURE_HOME="$1"; eval "$2"; '
            'jforge env repair --dry-run; printf "%s\\n" "${FIXTURE_HOOK-unset}"; '
            'jforge env repair --json; printf "%s\\n" "${FIXTURE_HOOK-unset}"; '
            'jforge env repair --shell=other.sh; printf "%s\\n" "${FIXTURE_HOOK-unset}"; '
            'jforge env repair; printf "%s\\n" "${FIXTURE_HOOK-unset}"')
    result = subprocess.run([shell, *args, "-ic", code, "test", str(tmp_path), hook],
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["unset", "unset", "unset", "refreshed"]


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("layout", ["legacy", "native-short"])
def test_spack_profile_activation_removes_old_managed_paths(base, tmp_path, shell, layout):
    if not shutil.which(shell):
        pytest.skip("shell is not installed")
    paths = []
    for name in ["first", "second"]:
        home = ForgeHome(base.root / "apt/profiles" / ("spack-" + name))
        home.initialize()
        write_json(
            home.root / "config.json",
            {**read_json(home.root / "config.json"), "apt_base_home": str(base.root)},
        )
        prefix = home.root / "spack/store/toy/bin" if layout == "legacy" else base.root / "apps" / name / "toy-1-abcdef123456/bin"
        prefix.mkdir(parents=True)
        env = tmp_path / (name + ".env")
        env.write_text(
            "export PATH="
            + shlex.quote(str(prefix))
            + ':"$PATH"\nexport FIXTURE_ROOT='
            + name
            + "\n"
        )
        out = tmp_path / (name + ".sh")
        out.write_text(activation(home, [env], extra_variables=["FIXTURE_ROOT"]))
        paths.append((out, str(prefix)))
    result = subprocess.run(
        [
            shell,
            "-c",
            'source "$1"; export PATH="/user/extra:$PATH"; source "$2"; source "$2"; printf "%s\\n%s" "$PATH" "$FIXTURE_ROOT"',
            shell,
            str(paths[0][0]),
            str(paths[1][0]),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert paths[0][1] not in result.stdout
    assert result.stdout.count(paths[1][1]) == 1 and "/user/extra" in result.stdout
    assert result.stdout.endswith("second")




def test_direct_root_option_requires_existing_spack(tmp_path, capsys):
    home = tmp_path / "absent"
    assert (
        main(["--home", str(home), "apt", "switch", "spack", "--direct", "--json"]) == 2
    )
    assert not home.exists()
