#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash jforge-pyenv-setup.sh [OPTIONS]
Install pyenv, Python and Jarvis-Forge under the current directory.
Source ./activate.sh after installation to load this installation.

Options
  --python <version>       Select a CPython patch version (default: 3.13.15).
  --install-build-deps     Install macOS build dependencies through Homebrew.
                          Linux prepares missing Python libraries locally by default.
  --upgrade               Upgrade Jarvis-Forge in the selected virtual environment.
  --shell-config          Register activate.sh in your Bash/Zsh startup files.
  --no-shell-config       Print a source command without editing startup files (default).
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
upgrade_package=false
configure_startup=false
while (( $# )); do
    case "$1" in
        --python)
            (( $# >= 2 )) || usage_error 'Expected a version after --python.'
            python_version=$2
            shift 2
            ;;
        --install-build-deps) install_deps=true; shift ;;
        --upgrade) upgrade_package=true; shift ;;
        --shell-config) configure_startup=true; shift ;;
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

bootstrap_root=$(pwd -P)
[[ "$bootstrap_root" != / ]] || fail 'Choose a dedicated installation directory.'
state_dir="$bootstrap_root/jforge-bootstrap"
# Serialize installers that share the launcher and activation file.
lock_dir="$bootstrap_root/.jforge-bootstrap.lock"
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

    prepare_linux_libraries
}

use_library() {
    local prefix=$1
    export CPPFLAGS="-I$prefix/include -I$prefix/include/ncursesw ${CPPFLAGS-}"
    export LDFLAGS="-L$prefix/lib -Wl,-rpath,$prefix/lib ${LDFLAGS-}"
    export PKG_CONFIG_PATH="$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
}

has_library() {
    local header=$1 expression=$2
    shift 2
    printf '#include <stdio.h>\n#include <%s>\nint main(void) { %s; return 0; }\n' "$header" "$expression" |
        "${CC:-cc}" ${CPPFLAGS-} ${LDFLAGS-} -x c - -o "$TMPDIR/library-probe" "$@" \
        > "$state_dir/logs/library-probe.log" 2>&1
}

library_loads() {
    "${CC:-cc}" ${LDFLAGS-} -x c - -o "$TMPDIR/library-loader" -ldl \
        > "$state_dir/logs/library-loader.log" 2>&1 <<'C' || return 1
#include <stdio.h>
#include <dlfcn.h>
int main(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        void *handle = dlopen(argv[i], RTLD_NOW | RTLD_LOCAL);
        if (!handle) {
            fprintf(stderr, "%s: %s\n", argv[i], dlerror());
            return 1;
        }
        dlclose(handle);
    }
    return 0;
}
C
    # Do not let shell preloads or library paths conceal broken dependencies.
    env -u LD_PRELOAD -u LD_LIBRARY_PATH "$TMPDIR/library-loader" "$@" \
        >> "$state_dir/logs/library-loader.log" 2>&1
}

local_library_works() {
    local prefix="$bootstrap_root/python-deps/$1"
    [[ ! -f "$prefix/.source-sha256" ]] || library_loads "$prefix/lib/"*.so
}

build_library() {
    local name=$1 url=$2 checksum=$3 source_name=$4
    shift 4
    local prefix="$bootstrap_root/python-deps/$name"
    local archive="$bootstrap_root/cache/python-deps/$source_name.tar.gz"
    local work log
    mkdir -p "$bootstrap_root/cache/python-deps"
    if [[ ! -f "$archive" ]] || [[ "$(archive_sha256 "$archive")" != "$checksum" ]]; then
        printf 'Downloading Python dependency %s\n' "$name" >&2
        if command -v curl >/dev/null; then
            curl --fail --location --proto '=https' --proto-redir '=https' \
                --connect-timeout 20 --max-time 300 --output "$archive.part" "$url"
        else
            wget --https-only --timeout=60 --tries=2 -O "$archive.part" "$url"
        fi
        [[ "$(archive_sha256 "$archive.part")" == "$checksum" ]] || fail "Checksum mismatch for $name."
        mv -f "$archive.part" "$archive"
    fi
    work=$(mktemp -d "$TMPDIR/dependency.XXXXXX")
    tar -xzf "$archive" -C "$work"
    log="$state_dir/logs/$name.log"
    printf 'Configure Python dependency %s · log %s\n' "$name" "$log" >&2
    (cd "$work/$source_name" && ./configure --prefix="$prefix" --libdir="$prefix/lib" "$@") \
        > "$log" 2>&1 || { tail -n 30 "$log" >&2; fail "Could not configure $name. Full log: $log"; }
    printf 'Build Python dependency %s\n' "$name" >&2
    make -C "$work/$source_name" -j4 >> "$log" 2>&1 ||
        { tail -n 30 "$log" >&2; fail "Could not build $name. Full log: $log"; }
    printf 'Install Python dependency %s\n' "$name" >&2
    make -C "$work/$source_name" install >> "$log" 2>&1 ||
        { tail -n 30 "$log" >&2; fail "Could not install $name. Full log: $log"; }
    use_library "$prefix"
    if ! library_loads "$prefix/lib/"*.so; then
        cat "$state_dir/logs/library-loader.log" >&2
        fail "Could not load $name independently. Full log: $state_dir/logs/library-loader.log"
    fi
    printf '%s\n' "$checksum" > "$prefix/.source-sha256"
    rm -rf -- "$work"
    printf 'Python dependency %s ready\n' "$name" >&2
}

archive_sha256() {
    if command -v sha256sum >/dev/null; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        shasum -a 256 "$1" | cut -d ' ' -f 1
    fi
}

prepare_linux_libraries() {
    local name
    command -v "${CC:-cc}" >/dev/null || fail 'A host C compiler is required to build Python.'
    command -v make >/dev/null || fail 'GNU make is required to build Python.'
    for name in ncurses-6.5 readline-8.3 sqlite-3.50.4; do
        if [[ -f "$bootstrap_root/python-deps/$name/.source-sha256" ]]; then
            use_library "$bootstrap_root/python-deps/$name"
        fi
    done
    if ! has_library curses.h '(void)initscr()' -lncursesw ||
        ! has_library panel.h '(void)new_panel((WINDOW *)0)' -lpanelw -lncursesw ||
        ! local_library_works ncurses-6.5; then
        build_library ncurses-6.5 https://ftp.gnu.org/gnu/ncurses/ncurses-6.5.tar.gz \
            136d91bc269a9a5785e5f9e980bc76ab57428f604ce3e5a5a90cebc767971cc6 ncurses-6.5 \
            --enable-widec --with-shared --without-normal --without-cxx --without-cxx-binding \
            --without-progs --without-tests --without-manpages --disable-db-install \
            --enable-pc-files --with-pkg-config-libdir="$bootstrap_root/python-deps/ncurses-6.5/lib/pkgconfig"
    fi
    if ! has_library readline/readline.h '(void)readline("")' -lreadline ||
        ! local_library_works readline-8.3; then
        build_library readline-8.3 https://ftp.gnu.org/gnu/readline/readline-8.3.tar.gz \
            fe5383204467828cd495ee8d1d3c037a7eba1389c22bc6a041f627976f9061cc readline-8.3 \
            --with-curses --with-shared-termcap-library
    fi
    if ! has_library sqlite3.h 'char required_version[(SQLITE_VERSION_NUMBER >= 3015002) ? 1 : -1]; (void)required_version; (void)sqlite3_libversion_number()' -lsqlite3 ||
        ! local_library_works sqlite-3.50.4; then
        build_library sqlite-3.50.4 https://www.sqlite.org/2025/sqlite-autoconf-3500400.tar.gz \
            a3db587a1b92ee5ddac2f66b3edb41b26f9c867275782d46c3a088977d6a5b18 sqlite-autoconf-3500400 \
            --disable-readline
    fi
}

pyenv_root=$bootstrap_root
private_pyenv="$bootstrap_root/bin/pyenv"
if [[ ! -x "$private_pyenv" ]]; then
    private_pyenv="$bootstrap_root/pyenv/bin/pyenv"
fi

pyenv_bin=$private_pyenv
base_python="$pyenv_root/versions/$python_version/bin/python"
mkdir -p "$bootstrap_root/tmp" "$bootstrap_root/cache/pip" "$state_dir/logs"
export TMPDIR="$bootstrap_root/tmp"
export PIP_CACHE_DIR="$bootstrap_root/cache/pip"

if [[ ! -x "$pyenv_bin" ]]; then
    printf 'Installing pyenv in %s\n' "$pyenv_root" >&2
    command -v git >/dev/null || fail 'Install Git and retry.'
    git -c advice.detachedHead=false clone --depth 1 --branch v2.8.14 --single-branch \
        https://github.com/pyenv/pyenv.git "$pyenv_root/pyenv" >&2
else
    printf 'Using pyenv at %s (root %s)\n' "$pyenv_bin" "$pyenv_root" >&2
fi

python_identity() {
"$base_python" -I - "$python_version" <<'PY'
import hashlib
import importlib
import os
import sys

if sys.implementation.name != "cpython" or ".".join(map(str, sys.version_info[:3])) != sys.argv[1]:
    sys.exit(f"Expected CPython {sys.argv[1]}, found {sys.version.split()[0]}.")
errors = []
for name in ("bz2", "ctypes", "curses", "curses.panel", "lzma", "readline", "sqlite3", "ssl", "zlib"):
    try:
        importlib.import_module(name)
    except ImportError as error:
        errors.append(f"{name}: {error}")
if errors:
    sys.exit("Python modules unavailable:\n  " + "\n  ".join(errors))
print(hashlib.sha256(os.fsencode(os.path.realpath(sys.executable))).hexdigest()[:16])
PY
}

if [[ ! -x "$base_python" ]] || ! python_id=$(python_identity 2> "$state_dir/logs/python-validation.log"); then
    if [[ "$platform" == Linux ]]; then
        prepare_linux_libraries
    elif "$install_deps"; then
        install_build_deps
    fi
    python_install_args=(--skip-existing)
    if [[ -x "$base_python" ]]; then
        printf 'Rebuilding incomplete CPython %s with pyenv\n' "$python_version" >&2
        python_install_args=(--force)
    else
        printf 'Building CPython %s with pyenv\n' "$python_version" >&2
    fi
    PYENV_ROOT="$pyenv_root" "$pyenv_bin" install "${python_install_args[@]}" "$python_version" >&2 ||
        fail "Could not install $python_version in $pyenv_root. See $state_dir/logs for library build logs."
    if ! python_id=$(python_identity 2> "$state_dir/logs/python-validation.log"); then
        cat "$state_dir/logs/python-validation.log" >&2
        fail "Python validation failed at $base_python. Check $state_dir/logs/python-validation.log."
    fi
fi

# Set the initial default in this installation, including after an interrupted build.
# Existing selections belong to the user.
if [[ ! -e "$pyenv_root/version" && ! -L "$pyenv_root/version" ]]; then
    PYENV_ROOT="$pyenv_root" "$pyenv_bin" global "$python_version"
fi

venv="$state_dir/venvs/$python_version-$python_id"
mkdir -p "$state_dir/bin"
if ! "$venv/bin/python" -I -m pip --version >/dev/null 2>&1; then
    "$base_python" -I -m venv "$venv"
fi
pip_args=(install)
if "$upgrade_package"; then
    pip_args+=(--upgrade)
fi
"$venv/bin/python" -I -m pip --require-virtualenv "${pip_args[@]}" Jarvis-Forge >&2

launcher="$state_dir/bin/jforge"
if [[ -e "$launcher" && ! -L "$launcher" ]]; then
    fail "Move the existing file at $launcher before retrying."
fi
launcher_target="$venv/bin/jforge-isolated"
launcher_tmp=$(mktemp "$venv/bin/.jforge.XXXXXX")
printf '#!/bin/bash\nexport JFORGE_HOME=${JFORGE_HOME:-%q}\nif [ "${1-}" = __jforge_controller_python ]; then\n    shift\n    exec %q -I -B "$@"\nfi\nexport JFORGE_CONTROLLER_LAUNCHER=%q\nexec %q -I -m jarvis_forge "$@"\n' \
    "$bootstrap_root/jforge" "$venv/bin/python" "$launcher" "$venv/bin/python" > "$launcher_tmp"
chmod u+x "$launcher_tmp"
"$launcher_tmp" --version
JFORGE_HOME="$bootstrap_root/jforge" XDG_CONFIG_HOME="$bootstrap_root/config" "$launcher_tmp" init >/dev/null
mv -f "$launcher_tmp" "$launcher_target"
ln -sfn "$launcher_target" "$launcher"

activation_file="$bootstrap_root/activate.sh"
[[ ! -d "$activation_file" ]] || fail "Move the existing directory at $activation_file before retrying."
activation_tmp=$(mktemp "$bootstrap_root/.activate.XXXXXX")
{
    printf 'export JFORGE_HOME=%q\n' "$bootstrap_root/jforge"
    printf 'export PIP_CACHE_DIR=%q\n' "$bootstrap_root/cache/pip"
    printf 'if [ -z "${PYENV_ROOT-}" ] || [ "$PYENV_ROOT" = %q ]; then\n' "$bootstrap_root"
    printf '    export PYENV_ROOT=%q\n' "$bootstrap_root"
    printf '    export PATH=%q:"$PATH"\n' "$(dirname "$private_pyenv")"
    printf '    eval "$(%q init -)"\n' "$private_pyenv"
    printf 'fi\n'
    printf '_jforge_bootstrap_bin=%q\n' "$state_dir/bin"
    cat <<'EOF'
# Add the Jforge launcher to PATH while keeping the current Python selection.
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

# Load the shell hook for package changes and successful interactive builds.
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
} > "$activation_tmp"
mv -f "$activation_tmp" "$activation_file"

if ! "$configure_startup"; then
    printf '\nActivate this installation with\n'
    printf 'source %q\n' "$activation_file"
    printf 'Then run: jforge apt profile\n'
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
printf 'Then run: jforge apt profile\n'
