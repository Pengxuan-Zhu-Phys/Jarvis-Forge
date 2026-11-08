#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash jforge-pyenv-setup.sh [OPTIONS]
Install Jarvis-Forge under ~/.jarvis-pyenv using an existing pyenv when available.

Options
  --python <version>       Select a CPython patch version (default: 3.13.15).
  --install-build-deps    Install Python build dependencies through Homebrew
                          or the Linux package manager if Python needs building.
  --no-shell-config       Leave startup files alone and print a source command.
  -h, --help              Show this message and exit.
EOF
}

fail() {
    printf 'jforge-pyenv-setup: %s\n' "$*" >&2
    exit 1
}

usage_error() {
    printf 'jforge-pyenv-setup: %s\nRun with --help for usage.\n' "$*" >&2
    exit 2
}

python_version=3.13.15
install_deps=false
configure_startup=true
while (( $# )); do
    case "$1" in
        --python)
            (( $# >= 2 )) || usage_error 'Expected a version after --python.'
            python_version=$2
            shift 2
            ;;
        --install-build-deps) install_deps=true; shift ;;
        --no-shell-config) configure_startup=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage_error "Unknown option: $1" ;;
    esac
done
[[ "$python_version" =~ ^3\.[1-9][0-9]+\.(0|[1-9][0-9]*)$ ]] ||
    usage_error 'Use a full CPython version, 3.10 or newer (for example, 3.13.15).'

login_shell=${SHELL:-/bin/bash}
login_shell=${login_shell##*/}
if "$configure_startup"; then
    case "$login_shell" in
        zsh|bash) ;;
        *) usage_error "Unsupported login shell: $login_shell. Use --no-shell-config for manual setup." ;;
    esac
fi
platform=$(uname -s)
case "$platform" in
    Darwin|Linux) ;;
    *) fail 'This installer supports macOS and Linux, including WSL.' ;;
esac

bootstrap_root="$HOME/.jarvis-pyenv"
[[ "$bootstrap_root" == /* ]] || fail 'HOME must be an absolute path.'
[[ ! -L "$bootstrap_root" ]] || fail "Use a directory at $bootstrap_root. This path is a symbolic link."
state_dir="$bootstrap_root/jforge-bootstrap"
# Serialize installers that share the launcher and activation file.
lock_dir="$bootstrap_root.jforge-bootstrap.lock"
mkdir -p "$(dirname "$lock_dir")"
mkdir "$lock_dir" || fail "Another installation may be running. If it was interrupted, remove $lock_dir and retry."
trap 'rmdir "$lock_dir"' EXIT

install_build_deps() {
    if [[ "$platform" == Darwin ]]; then
        xcode-select -p >/dev/null 2>&1 || fail 'Run xcode-select --install, then retry.'
        if ! command -v brew >/dev/null; then
            if [[ -x /opt/homebrew/bin/brew ]]; then
                export PATH="/opt/homebrew/bin:$PATH"
            elif [[ -x /usr/local/bin/brew ]]; then
                export PATH="/usr/local/bin:$PATH"
            else
                fail 'Install Homebrew from https://brew.sh/ and retry.'
            fi
        fi
        brew install openssl@3 readline sqlite xz tcl-tk@8 pkgconf >&2
        return
    fi

    local admin=(env)
    if (( EUID != 0 )); then
        command -v sudo >/dev/null || fail 'Ask your administrator to install Python build dependencies.'
        admin=(sudo)
    fi
    local fetch_tools=(git)
    if ! command -v curl >/dev/null && ! command -v wget >/dev/null; then
        fetch_tools+=(curl)
    fi
    if command -v apt-get >/dev/null; then
        "${admin[@]}" apt-get update >&2
        "${admin[@]}" apt-get install -y build-essential patch libssl-dev zlib1g-dev \
            libbz2-dev libreadline-dev libsqlite3-dev libncurses-dev xz-utils tk-dev \
            libffi-dev liblzma-dev "${fetch_tools[@]}" >&2
    elif command -v dnf >/dev/null || command -v yum >/dev/null; then
        local rpm_manager
        rpm_manager=$(type -P dnf || type -P yum)
        "${admin[@]}" "$rpm_manager" install -y gcc make patch tar xz openssl-devel zlib-devel \
            bzip2-devel readline-devel sqlite-devel ncurses-devel xz-devel \
            libffi-devel "${fetch_tools[@]}" >&2
    elif command -v zypper >/dev/null; then
        "${admin[@]}" zypper --non-interactive install gcc make patch xz openssl-devel \
            zlib-devel libbz2-devel readline-devel sqlite3-devel ncurses-devel xz-devel \
            libffi-devel "${fetch_tools[@]}" >&2
    elif command -v pacman >/dev/null; then
        "${admin[@]}" pacman -S --needed --noconfirm base-devel openssl zlib bzip2 \
            readline sqlite ncurses xz libffi "${fetch_tools[@]}" >&2
    elif command -v apk >/dev/null; then
        "${admin[@]}" apk add --no-cache build-base linux-headers patch xz openssl-dev zlib-dev \
            bzip2-dev readline-dev sqlite-dev ncurses-dev xz-dev libffi-dev \
            "${fetch_tools[@]}" >&2
    else
        fail 'Install Python build dependencies for your system and retry.'
    fi
}

pyenv_bin=
pyenv_root=
base_python=
private_pyenv="$bootstrap_root/bin/pyenv"
if [[ ! -x "$private_pyenv" ]]; then
    private_pyenv="$bootstrap_root/pyenv/bin/pyenv"
fi

# Remember the first working pyenv, but prefer one that already has the requested Python.
consider_pyenv() {
    local candidate=$1 root_hint=$2 candidate_root candidate_python
    [[ -x "$candidate" ]] || return 1
    candidate_root=$(PYENV_ROOT="$root_hint" "$candidate" root) || return 1
    [[ "$candidate_root" == /* ]] || return 1
    candidate_root=${candidate_root%/}
    candidate_python="$candidate_root/versions/$python_version/bin/python"
    if [[ -z "$pyenv_bin" || -x "$candidate_python" ]]; then
        pyenv_bin=$candidate
        pyenv_root=$candidate_root
        base_python=$candidate_python
    fi
    [[ -x "$candidate_python" ]]
}

configured_root=${PYENV_ROOT-}
configured_root=${configured_root%/}
if [[ "$configured_root" == "$bootstrap_root" ]]; then
    configured_root=
fi
if [[ -n "$configured_root" ]]; then
    consider_pyenv "$configured_root/bin/pyenv" "$configured_root" || :
fi
if [[ ! -x "$base_python" ]]; then
    while IFS= read -r candidate; do
        [[ "$candidate" -ef "$private_pyenv" ]] && continue
        if consider_pyenv "$candidate" "$configured_root"; then
            break
        fi
    done < <(
        type -aP pyenv || :
        printf '%s\n' "$HOME/.local/bin/pyenv" /usr/local/bin/pyenv /usr/bin/pyenv
        if [[ "$platform" == Darwin ]]; then
            printf '%s\n' /opt/homebrew/bin/pyenv
        else
            printf '%s\n' /home/linuxbrew/.linuxbrew/bin/pyenv "$HOME/.linuxbrew/bin/pyenv"
        fi
    )
fi
if [[ ! -x "$base_python" ]]; then
    consider_pyenv "$HOME/.pyenv/bin/pyenv" "$HOME/.pyenv" || :
fi
if [[ ! -x "$base_python" && -n "$pyenv_bin" && "$pyenv_root" != "$HOME/.pyenv" ]]; then
    consider_pyenv "$pyenv_bin" "$HOME/.pyenv" || :
fi
if [[ ! -x "$base_python" ]]; then
    consider_pyenv "$private_pyenv" "$bootstrap_root" || :
fi

if [[ ! -x "$base_python" ]] && "$install_deps"; then
    install_build_deps
fi
new_pyenv=false
if [[ -z "$pyenv_bin" ]]; then
    pyenv_root=$bootstrap_root
    pyenv_bin=$private_pyenv
    base_python="$pyenv_root/versions/$python_version/bin/python"
    printf 'Installing pyenv in %s\n' "$pyenv_root" >&2
    command -v git >/dev/null || fail 'Install Git and retry.'
    git -c advice.detachedHead=false clone --depth 1 --branch v2.8.14 --single-branch \
        https://github.com/pyenv/pyenv.git "$pyenv_root/pyenv" >&2
    new_pyenv=true
else
    printf 'Using pyenv at %s (root %s)\n' "$pyenv_bin" "$pyenv_root" >&2
fi

if [[ ! -x "$base_python" ]]; then
    printf 'Building CPython %s with pyenv\n' "$python_version" >&2
    PYENV_ROOT="$pyenv_root" "$pyenv_bin" install --skip-existing "$python_version" >&2 ||
        fail "Could not install $python_version in $pyenv_root. For missing build dependencies, retry with --install-build-deps. If pyenv does not know this version, update pyenv or choose an installed version with --python."
fi
python_id=$("$base_python" -I - "$python_version" <<'PY'
import hashlib
import os
import sys

if sys.implementation.name != "cpython" or ".".join(map(str, sys.version_info[:3])) != sys.argv[1]:
    sys.exit(f"Expected CPython {sys.argv[1]}, found {sys.version.split()[0]}.")
try:
    import bz2, ctypes, lzma, sqlite3, ssl, zlib
except ImportError as error:
    sys.exit(str(error))
print(hashlib.sha256(os.fsencode(os.path.realpath(sys.executable))).hexdigest()[:16])
PY
) || fail "Python validation failed at $base_python. For an incomplete build, install the missing development libraries and rebuild that version with pyenv, or select a working version with --python."

# Set a default only for a new, private pyenv. Existing selections belong to the user.
if "$new_pyenv" && [[ ! -e "$pyenv_root/version" && ! -L "$pyenv_root/version" ]]; then
    PYENV_ROOT="$pyenv_root" "$pyenv_bin" global "$python_version"
fi

venv="$state_dir/venvs/$python_version"
if [[ "$pyenv_root" != "$bootstrap_root" ]]; then
    # Keep venvs separate when the same Python version comes from different roots.
    venv="$venv-$python_id"
fi
mkdir -p "$state_dir/bin"
if ! "$venv/bin/python" -I -m pip --version >/dev/null 2>&1; then
    "$base_python" -I -m venv "$venv"
fi
"$venv/bin/python" -I -m pip --require-virtualenv install Jarvis-Forge >&2
"$venv/bin/jforge" --version

launcher="$state_dir/bin/jforge"
if [[ -e "$launcher" && ! -L "$launcher" ]]; then
    fail "Move the existing file at $launcher before retrying."
fi
ln -sfn "$venv/bin/jforge" "$launcher"

activation_file="$state_dir/activate.sh"
[[ ! -d "$activation_file" ]] || fail "Move the existing directory at $activation_file before retrying."
activation_tmp=$(mktemp "$state_dir/.activate.XXXXXX")
cat > "$activation_tmp" <<'EOF'
# Add the Jforge launcher to PATH while keeping the current Python selection.
_jforge_bootstrap_bin="$HOME/.jarvis-pyenv/jforge-bootstrap/bin"
_jforge_bootstrap_path=${PATH:+":$PATH:"}
case "$_jforge_bootstrap_path" in
    ":$_jforge_bootstrap_bin:"*) ;;
    *)
        # Re-sourcing after package activation can find the launcher later in PATH.
        while [ "${_jforge_bootstrap_path#*":$_jforge_bootstrap_bin:"}" != "$_jforge_bootstrap_path" ]; do
            _jforge_bootstrap_path="${_jforge_bootstrap_path%%":$_jforge_bootstrap_bin:"*}:${_jforge_bootstrap_path#*":$_jforge_bootstrap_bin:"}"
        done
        export PATH="$_jforge_bootstrap_bin${_jforge_bootstrap_path%:}"
        ;;
esac
unset _jforge_bootstrap_bin _jforge_bootstrap_path

# Load the selected package environment and refresh it after interactive apt changes.
case "${0##*/}:$-" in
    sh:*|-sh:*) ;;
    *:*i*)
        if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ] &&
            _jforge_bootstrap_hook=$(command jforge apt shell-init); then
            eval "$_jforge_bootstrap_hook"
        fi
        unset _jforge_bootstrap_hook
        ;;
esac
EOF
mv -f "$activation_tmp" "$activation_file"

if ! "$configure_startup"; then
    printf '\nAdd this line to ~/.bashrc (or ~/.zshrc for Zsh)\n'
    printf 'source "%s"\n' "\$HOME/.jarvis-pyenv/jforge-bootstrap/activate.sh"
    printf 'Then open a new terminal and run: jforge --help\n'
    exit 0
fi

# The generated startup block expands its variables in the user's shell.
# shellcheck disable=SC2016
configure_shell() {
    local rc=$1
    local interactive_only=${2:-false}
    local start_marker='# >>> jforge pyenv setup >>>'
    local end_marker='# <<< jforge pyenv setup <<<'
    local block
    block=$(
        printf '%s\n' "$start_marker"
        if "$interactive_only"; then
            printf 'case $- in *i*)\n'
        fi
        if "$new_pyenv" ||
            { [[ -x "$private_pyenv" && -f "$rc" ]] &&
                grep -Fqx "$(printf '            export PYENV_ROOT=%q' "$bootstrap_root")" "$rc"; }; then
            printf 'if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ] &&\n'
            printf '    { [ -z "${PYENV_ROOT-}" ] || [ "$PYENV_ROOT" = %q ]; } &&\n' "$bootstrap_root"
            printf '    ! typeset -f pyenv >/dev/null 2>&1; then\n'
            printf '    case "${0##*/}:$PATH:" in\n'
            printf '        sh:*|-sh:*|*:"$HOME/.pyenv/shims":*) ;;\n'
            printf '        *)\n'
            printf '            export PYENV_ROOT=%q\n' "$bootstrap_root"
            printf '            export PATH=%q:"$PATH"\n' "$(dirname "$private_pyenv")"
            printf '            eval "$(pyenv init - %s)"\n' "$login_shell"
            printf '            ;;\n'
            printf '    esac\n'
            printf 'fi\n'
        fi
        printf 'if [ -r %q ]; then . %q; fi\n' "$activation_file" "$activation_file"
        if "$interactive_only"; then
            printf ';; esac\n'
        fi
        printf '%s\n' "$end_marker"
    )
    "$base_python" -I - "$rc" "$block" "$start_marker" "$end_marker" <<'PY'
import os
from pathlib import Path
import stat
import sys
from tempfile import NamedTemporaryFile

rc = Path(sys.argv[1]).resolve()
block, start, end = map(os.fsencode, sys.argv[2:])
current = rc.read_bytes() if rc.exists() else b""
lines = current.splitlines(keepends=True)
starts = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == start]
ends = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == end]
if not starts and not ends:
    updated = current + (b"\n" if current else b"") + block + b"\n"
elif len(starts) == len(ends) == 1 and starts[0] < ends[0]:
    updated = b"".join(lines[:starts[0]] + [block + b"\n"] + lines[ends[0] + 1:])
else:
    sys.exit(f"Remove the incomplete or duplicated jforge pyenv setup block in {rc} and retry.")

if updated != current:
    rc.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(rc.stat().st_mode) if rc.exists() else 0o600
    with NamedTemporaryFile(dir=rc.parent, delete=False) as temp:
        replacement = Path(temp.name)
    try:
        replacement.write_bytes(updated)
        replacement.chmod(mode)
        replacement.replace(rc)
    finally:
        replacement.unlink(missing_ok=True)
PY
}

if [[ "$login_shell" == zsh ]]; then
    zsh_config=$(zsh -c 'print -r -- "${ZDOTDIR:-$HOME}"')
    configure_shell "$zsh_config/.zshrc"
else
    login_rc="$HOME/.bash_profile"
    for candidate in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
        if [[ -r "$candidate" ]]; then
            login_rc=$candidate
            break
        fi
    done
    configure_shell "$HOME/.bashrc" true
    configure_shell "$login_rc"
fi

printf '\nOpen a new terminal or run: exec %s -l\n' "$login_shell"
printf 'Then run: jforge --help\n'
