#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash jforge-pyenv-setup.sh [OPTIONS]
Install pyenv, Python and the latest PyPI Jarvis-Forge release under the current directory.
Source ./activate.sh after installation to load this installation.

Options
  --home <path>            Initial Forge data home (default: ./jforge; saved separately).
  --pyenv-root <path>      Reuse or install a separate pyenv root (default: current directory).
  --stage <path>           Python build scratch root (default: node temporary directory).
  --python <version>       Select a CPython patch version (default: 3.13.15).
  --install-build-deps     Install macOS build dependencies through Homebrew.
                          Linux prepares missing Python libraries locally by default.
  --upgrade               Install the latest Jarvis-Forge release (the default).
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
configure_startup=false
forge_home_option=
pyenv_root_option=
stage_option=
while (( $# )); do
    case "$1" in
        --python)
            (( $# >= 2 )) || usage_error 'Expected a version after --python.'
            python_version=$2
            shift 2
            ;;
        --home|--pyenv-root|--stage)
            (( $# >= 2 )) && [[ -n "$2" ]] || usage_error "Expected a path after $1."
            case "$1" in
                --home) forge_home_option=$2 ;;
                --pyenv-root) pyenv_root_option=$2 ;;
                --stage) stage_option=$2 ;;
            esac
            shift 2
            ;;
        --install-build-deps) install_deps=true; shift ;;
        --upgrade) shift ;;
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
cleanup() {
    local status=$?
    if [[ -n "${build_stage-}" ]]; then
        for log in "$build_stage"/python-build*.log; do
            [[ ! -f "$log" ]] || cp "$log" "$state_dir/logs/$(basename "$log")"
        done
        if (( status == 0 )); then
            rm -rf -- "$build_stage"
        else
            printf 'Build scratch retained at %s\n' "$build_stage" >&2
        fi
    fi
    rmdir "$lock_dir"
}
trap cleanup EXIT

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
    make -C "$work/$source_name" -j"$build_jobs" >> "$log" 2>&1 ||
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

preflight_linux_libraries() {
    local missing=()
    # Detect every non-bootstrapped required extension before any Python build.
    # The three libraries prepared below are checked after their local fallback.
    command -v "${CC:-cc}" >/dev/null || fail 'A host C compiler is required to build Python.'
    command -v make >/dev/null || fail 'GNU make is required to build Python.'
    has_library zlib.h '(void)zlibVersion()' -lz || missing+=("zlib (zlib.h, -lz; Python zlib)")
    has_library bzlib.h '(void)BZ2_bzlibVersion()' -lbz2 || missing+=("bzip2 (bzlib.h, -lbz2; Python bz2)")
    has_library lzma.h '(void)lzma_version_number()' -llzma || missing+=("xz (lzma.h, -llzma; Python lzma)")
    has_library ffi.h '(void)ffi_prep_cif((ffi_cif *)0, FFI_DEFAULT_ABI, 0, &ffi_type_void, (ffi_type **)0)' -lffi ||
        missing+=("libffi (ffi.h, -lffi; Python ctypes)")
    has_library openssl/ssl.h 'char minimum_openssl[(OPENSSL_VERSION_NUMBER >= 0x10101000L) ? 1 : -1]; (void)minimum_openssl; (void)TLS_method()' -lssl -lcrypto ||
        missing+=("OpenSSL >= 1.1.1 (openssl/ssl.h, -lssl -lcrypto; Python ssl)")
    if (( ${#missing[@]} )); then
        printf 'Missing Python build dependencies:\n' >&2
        printf '  %s\n' "${missing[@]}" >&2
        fail 'Prepare these development libraries before retrying; existing user libraries can be selected through CPPFLAGS/LDFLAGS/PKG_CONFIG_PATH. Python was not rebuilt.'
    fi
}

available_build_jobs() {
    local proc=${1:-/proc} count allowed kind root mount controllers selected group directory quota period limit
    count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf 1)
    [[ "$count" =~ ^[1-9][0-9]*$ ]] || count=1
    if [[ -r "$proc/self/status" ]]; then
        allowed=$(awk '/^Cpus_allowed_list:/ {n=split($2,a,","); total=0; for(i=1;i<=n;i++){m=split(a[i],r,"-"); total+=(m==2?r[2]-r[1]+1:1)} print total}' "$proc/self/status")
        if [[ "$allowed" =~ ^[1-9][0-9]*$ ]] && (( allowed < count )); then count=$allowed; fi
    fi
    if [[ -r "$proc/self/cgroup" && -r "$proc/self/mountinfo" ]]; then
        while IFS=' ' read -r kind root mount controllers; do
            root=${root//\\040/ }; mount=${mount//\\040/ }
            while IFS=: read -r _ selected group; do
                if [[ "$kind" == cgroup2 ]]; then
                    [[ -z "$selected" ]] || continue
                else
                    [[ ",$selected," == *,cpu,* && ",$controllers," == *,cpu,* ]] || continue
                fi
                directory=$mount
                if [[ "$group" == "$root"/* && "$group" != *..* ]]; then
                    directory="$mount/${group#"$root"/}"
                elif [[ "$root" == / && "$group" != *..* ]]; then
                    directory="$mount$group"
                fi
                while :; do
                    quota=max; period=1
                    if [[ "$kind" == cgroup2 && -r "$directory/cpu.max" ]]; then
                        read -r quota period < "$directory/cpu.max"
                    elif [[ "$kind" == cgroup && -r "$directory/cpu.cfs_quota_us" && -r "$directory/cpu.cfs_period_us" ]]; then
                        read -r quota < "$directory/cpu.cfs_quota_us"
                        read -r period < "$directory/cpu.cfs_period_us"
                    fi
                    if [[ "$quota" =~ ^[1-9][0-9]*$ && "$period" =~ ^[1-9][0-9]*$ ]]; then
                        limit=$(( (quota + period - 1) / period ))
                        if (( limit < count )); then count=$limit; fi
                    fi
                    [[ "$directory" != "$mount" ]] || break
                    directory=${directory%/*}
                done
            done < "$proc/self/cgroup"
        done < <(awk '{for(i=1;i<=NF;i++) if($i=="-" && ($(i+1)=="cgroup" || $(i+1)=="cgroup2")) print $(i+1),$4,$5,$(i+3)}' "$proc/self/mountinfo")
    fi
    (( count <= 16 )) || count=16
    printf '%s\n' "$count"
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

pyenv_root=${pyenv_root_option:-$bootstrap_root}
mkdir -p "$pyenv_root"
pyenv_root=$(cd "$pyenv_root" && pwd -P)
private_pyenv="$pyenv_root/bin/pyenv"
if [[ ! -x "$private_pyenv" ]]; then
    private_pyenv="$pyenv_root/pyenv/bin/pyenv"
fi

pyenv_bin=$private_pyenv
base_python="$pyenv_root/versions/$python_version/bin/python"
mkdir -p "$bootstrap_root/cache/pip" "$bootstrap_root/cache/python" "$state_dir/logs"
stage_root=${stage_option:-${SLURM_TMPDIR:-${TMPDIR:-/tmp}}}
mkdir -p "$stage_root"
stage_root=$(cd "$stage_root" && pwd -P)
build_stage=$(mktemp -d "$stage_root/jforge-python.XXXXXXXX")
export TMPDIR="$build_stage"
export PYTHON_BUILD_CACHE_PATH="$bootstrap_root/cache/python"
build_jobs=$(available_build_jobs)
export MAKE_OPTS="-j$build_jobs"
export MAKE_INSTALL_OPTS="-j$build_jobs"
printf 'Python build policy: %s jobs · scratch %s\n' "$build_jobs" "$build_stage" >&2
export PIP_CACHE_DIR="$bootstrap_root/cache/pip"

if [[ ! -x "$pyenv_bin" ]]; then
    printf 'Installing pyenv in %s\n' "$pyenv_root" >&2
    command -v git >/dev/null || fail 'Install Git and retry.'
    git -c advice.detachedHead=false clone --depth 1 --branch v2.8.14 --single-branch \
        https://github.com/pyenv/pyenv.git "$pyenv_root/pyenv" >&2
else
    printf 'Using pyenv at %s (root %s)\n' "$pyenv_bin" "$pyenv_root" >&2
fi

validate_python() {
"$base_python" -I - "$python_version" <<'PY'
import importlib
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
PY
}

if [[ ! -x "$base_python" ]] || ! validate_python 2> "$state_dir/logs/python-validation.log"; then
    if [[ "$platform" == Linux ]]; then
        preflight_linux_libraries
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
    if ! validate_python 2> "$state_dir/logs/python-validation.log"; then
        cat "$state_dir/logs/python-validation.log" >&2
        fail "Python validation failed at $base_python. Check $state_dir/logs/python-validation.log."
    fi
fi

# Set the initial default in this installation, including after an interrupted build.
# Existing selections belong to the user.
if [[ ! -e "$pyenv_root/version" && ! -L "$pyenv_root/version" ]]; then
    PYENV_ROOT="$pyenv_root" "$pyenv_bin" global "$python_version"
fi

mkdir -p "$state_dir/bin"
# Validate the selected data home before pip can change the installed client.
forge_home_option=$("$base_python" -I - "$bootstrap_root" "$forge_home_option" <<'PY'
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
selected = sys.argv[2] or os.environ.get("JFORGE_HOME")
saved = root / "config/jarvis-forge/default-home.json"
if not selected and saved.exists():
    record = json.loads(saved.read_text())
    selected = record.get("default_home")
    if record.get("schema_version") != 1 or not isinstance(selected, str) or not Path(selected).is_absolute():
        sys.exit(f"Invalid remembered Forge home: {saved}")
home = Path(selected or root / "jforge").expanduser().resolve()
if Path(sys.prefix).resolve().is_relative_to(home) or Path(sys.base_prefix).resolve().is_relative_to(home):
    sys.exit("Forge home must be separate from the pyenv Python installation; select another --home directory.")
config = home / "config.json"
if config.exists():
    record = json.loads(config.read_text())
    if record.get("schema_version") != 1 or record.get("layout_version") != 4:
        sys.exit(f"Unsupported Forge layout at {home}. Repeat setup with --home /path/to/new-forge; pyenv does not need rebuilding.")
print(home)
PY
)
"$base_python" -I -m pip install --no-user --upgrade --no-cache-dir Jarvis-Forge >&2
PYENV_ROOT="$pyenv_root" "$pyenv_bin" rehash

# Publish installer-owned entries; Forge does not manage or repair this Python setup.
"$base_python" -I - "$bootstrap_root" "$pyenv_root" "$pyenv_bin" "$base_python" "$forge_home_option" "$build_jobs" "$stage_root" <<'PY_SETUP'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ACTIVATION_HEADER = "# Jarvis-Forge bootstrap activation v1\n"
LAUNCHER_HEADER = "#!/bin/bash\n# Jarvis-Forge bootstrap launcher\n"

def launcher(data):
    quote = shlex.quote
    return (LAUNCHER_HEADER + f'export JFORGE_CONFIG_HOME={quote(data["config_home"])}\n'
            'if [ "${1-}" = __jforge_controller_python ]; then\n    shift\n'
            f'    exec {quote(data["python"])} -I -B "$@"\nfi\n'
            f'export JFORGE_CONTROLLER_LAUNCHER={quote(data["launcher"])}\n'
            f'exec {quote(data["python"])} -I -m jarvis_forge "$@"\n')

def activation(data):
    quote = shlex.quote
    return (ACTIVATION_HEADER + f'export JFORGE_CONFIG_HOME={quote(data["config_home"])}\n'
            f'export PIP_CACHE_DIR={quote(str(Path(data["root"]) / "cache/pip"))}\n' + r'''
_jforge_bootstrap_prepend() {
    local _jfp_bin=$1 _jfp_path
    _jfp_path=${PATH:+":$PATH:"}
    while [ "${_jfp_path#*":$_jfp_bin:"}" != "$_jfp_path" ]; do
        _jfp_path="${_jfp_path%%":$_jfp_bin:"*}:${_jfp_path#*":$_jfp_bin:"}"
    done
    export PATH="$_jfp_bin${_jfp_path%:}"
}
''' +
            f'if [ -z "${{PYENV_ROOT-}}" ] || [ "$PYENV_ROOT" = {quote(data["pyenv_root"])} ]; then\n'
            f'    export PYENV_ROOT={quote(data["pyenv_root"])}\n'
            f'    _jforge_bootstrap_prepend {quote(str(Path(data["pyenv"]).parent))}\n'
            f'    if _jforge_pyenv_init=$({quote(data["pyenv"])} init -); then\n'
            '        eval "$_jforge_pyenv_init" || printf "%s\\n" "jforge: Could not initialize pyenv." >&2\n'
            '    else\n        printf "%s\\n" "jforge: Could not initialize pyenv." >&2\n    fi\n'
            '    unset _jforge_pyenv_init\nfi\n'
            f'_jforge_bootstrap_prepend {quote(str(Path(data["launcher"]).parent))}\n'
            'unset -f _jforge_bootstrap_prepend\n' + r'''
case "${0##*/}:$-" in
    sh:*|-sh:*) ;;
    *:*i*)
        if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ] &&
            _jforge_bootstrap_hook=$(command jforge shell-init); then
            eval "$_jforge_bootstrap_hook" || printf '%s\n' 'jforge: Could not load the shell hook.' >&2
        fi
        unset _jforge_bootstrap_hook
        ;;
esac
''')

root = Path(sys.argv[1])
data = {"schema_version": 1, "kind": "pyenv-installer", "root": str(root),
        "pyenv_root": sys.argv[2], "pyenv": sys.argv[3], "python": sys.argv[4],
        "python_version": sys.version.split()[0], "initial_forge_home": sys.argv[5],
        "launcher": str(root / "jforge-bootstrap/bin/jforge"),
        "activation": str(root / "activate.sh"), "config_home": str(root / "config"),
        "build": {"jobs": int(sys.argv[6]), "stage": sys.argv[7]}}
for key, header in (("launcher", LAUNCHER_HEADER), ("activation", ACTIVATION_HEADER)):
    path = Path(data[key])
    if path.is_symlink() or path.exists() and (not path.is_file() or not path.read_text().startswith(header)):
        sys.exit(f"Choose a clean installer directory; unrecognized entry: {path}")

def write(path, text, mode=0o600):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

entry = Path(data["launcher"])
fd, temporary = tempfile.mkstemp(dir=entry.parent)
os.close(fd)
try:
    Path(temporary).write_text(launcher(data))
    Path(temporary).chmod(0o700)
    subprocess.run([temporary, "--home", sys.argv[5], "init"], check=True, stdout=subprocess.DEVNULL)
    os.replace(temporary, entry)
finally:
    Path(temporary).unlink(missing_ok=True)
write(Path(data["activation"]), activation(data))
write(root / "jforge-bootstrap/metadata.json", json.dumps(data, indent=2) + "\n")
PY_SETUP
launcher="$state_dir/bin/jforge"
activation_file="$bootstrap_root/activate.sh"
"$launcher" --version

if ! "$configure_startup"; then
    printf '\nActivate this installation with\n'
    printf 'source %q\n' "$activation_file"
    if [[ "$platform" == Darwin ]]; then printf 'Then run: jforge profile check macos\n'; else printf 'Then run: jforge profile check ubuntu24\n'; fi
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
if [[ "$platform" == Darwin ]]; then printf 'Then run: jforge profile check macos\n'; else printf 'Then run: jforge profile check ubuntu24\n'; fi
