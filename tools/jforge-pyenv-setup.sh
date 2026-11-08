#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash jforge-pyenv-setup.sh [OPTIONS]
Install pyenv, CPython and Jarvis-Forge under ~/.jarvis-pyenv.

Options
  --python <version>       Select a CPython patch version (default: 3.13.15).
  --install-build-deps    Install system build dependencies if Python needs
                          building (Homebrew, apt or dnf; apt/dnf may use sudo).
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
case "$(uname -s)" in
    Darwin|Linux) ;;
    *) fail 'This installer supports macOS and Linux, including WSL.' ;;
esac

export PYENV_ROOT="$HOME/.jarvis-pyenv"
[[ "$PYENV_ROOT" == /* ]] || fail 'PYENV_ROOT must be an absolute path.'
[[ ! -L "$PYENV_ROOT" ]] || fail "Use a directory at $PYENV_ROOT. This path is a symbolic link."
state_dir="$PYENV_ROOT/jforge-bootstrap"
# Keep the lock outside PYENV_ROOT so a first-time git clone has an empty target.
lock_dir="${PYENV_ROOT%/}.jforge-bootstrap.lock"
mkdir -p "$(dirname "$lock_dir")"
mkdir "$lock_dir" || fail "Another installation may be running. If it was interrupted, remove $lock_dir and retry."
trap 'rmdir "$lock_dir"' EXIT

install_build_deps() {
    if [[ "$(uname -s)" == Darwin ]]; then
        xcode-select -p >/dev/null 2>&1 || fail 'Run xcode-select --install, then retry.'
        command -v brew >/dev/null || fail 'Install Homebrew from https://brew.sh/ and retry.'
        brew install openssl@3 readline sqlite xz tcl-tk@8 pkgconf >&2
        return
    fi

    local admin=(env)
    if (( EUID != 0 )); then
        command -v sudo >/dev/null || fail 'Ask your administrator to install Python build dependencies.'
        admin=(sudo)
    fi
    if command -v apt-get >/dev/null; then
        "${admin[@]}" apt-get update >&2
        "${admin[@]}" apt-get install -y build-essential curl git libssl-dev zlib1g-dev \
            libbz2-dev libreadline-dev libsqlite3-dev libncurses-dev xz-utils tk-dev \
            libffi-dev liblzma-dev >&2
    elif command -v dnf >/dev/null; then
        "${admin[@]}" dnf install -y gcc make patch curl git openssl-devel zlib-devel \
            bzip2-devel readline-devel sqlite-devel ncurses-devel xz-devel \
            libffi-devel >&2
    else
        fail 'Install Python build dependencies for your system and retry.'
    fi
}

base_python="$PYENV_ROOT/versions/$python_version/bin/python"
if [[ ! -x "$base_python" ]] && "$install_deps"; then
    install_build_deps
fi
pyenv_bin="$PYENV_ROOT/bin/pyenv"
if [[ ! -x "$pyenv_bin" ]]; then
    printf 'Installing pyenv in %s\n' "$PYENV_ROOT" >&2
    command -v git >/dev/null || fail 'Install Git and retry.'
    git -c advice.detachedHead=false clone --depth 1 --branch v2.8.14 --single-branch \
        https://github.com/pyenv/pyenv.git "$PYENV_ROOT" >&2
fi

if [[ ! -x "$base_python" ]]; then
    printf 'Building CPython %s with pyenv\n' "$python_version" >&2
    "$pyenv_bin" install "$python_version" >&2 ||
        fail "If build dependencies are missing, retry with --install-build-deps. If pyenv does not know $python_version, update pyenv or choose an available version with --python."
fi
"$base_python" -I -c 'import bz2, ctypes, lzma, sqlite3, ssl, zlib' ||
    fail "The pyenv Python at $base_python is missing standard modules. Install the build dependencies and rebuild it with pyenv, or choose another version with --python."

venv="$state_dir/venvs/$python_version"
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
case ":${PATH-}:" in
    ":$HOME/.jarvis-pyenv/jforge-bootstrap/bin:"*) ;;
    *) export PATH="$HOME/.jarvis-pyenv/jforge-bootstrap/bin${PATH:+:$PATH}" ;;
esac
EOF
mv -f "$activation_tmp" "$activation_file"

# Initialize the default only when pyenv has no saved global selection.
if [[ ! -e "$PYENV_ROOT/version" && ! -L "$PYENV_ROOT/version" ]]; then
    "$pyenv_bin" global "$python_version"
fi

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
        printf 'if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ] &&\n'
        printf '    { [ -z "${PYENV_ROOT-}" ] || [ "$PYENV_ROOT" = %q ]; } &&\n' "$PYENV_ROOT"
        printf '    ! typeset -f pyenv >/dev/null 2>&1; then\n'
        printf '    case "${0##*/}:$PATH:" in\n'
        printf '        sh:*|-sh:*|*:"$HOME/.pyenv/shims":*) ;;\n'
        printf '        *)\n'
        printf '            export PYENV_ROOT=%q\n' "$PYENV_ROOT"
        printf '            export PATH=%q:"$PATH"\n' "$PYENV_ROOT/bin"
        printf '            eval "$(pyenv init - %s)"\n' "$login_shell"
        printf '            ;;\n'
        printf '    esac\n'
        printf 'fi\n'
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
