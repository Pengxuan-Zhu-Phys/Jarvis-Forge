"""NUL-delimited shell actions on fd 9, separate from stdout and JSON results."""

import os
import sys

from .storage import ForgeError


def requested():
    return os.environ.get("_JFORGE_SHELL_HANDOFF") == "1"


def send(action, argument=""):
    if action not in {"source", "build", "detach", "hook"}:
        raise ValueError(f"Unknown shell handoff action: {action}")
    if "\0" in str(argument):
        raise ValueError("Shell handoff paths cannot contain NUL")
    with open(9, "wb", closefd=False) as stream:
        stream.write(os.fsencode(action) + b"\0" + os.fsencode(argument) + b"\0")
    return True


def for_result(ctx, command, values):
    """Commands decide their shell effect after successful execution, never the hook."""
    if not requested() or ctx.obj.as_json or values.get("dry_run") or ctx.params.get("dry_run"):
        return
    commands = {"env detach", "env switch", "env drop", "env repair", "apt profile apps", "profile use"}
    apt_change = command.startswith("apt ") and (values.get("state_committed") or (
        command == "apt profile global" and values.get("status") == "selected"))
    if command not in commands and not apt_change:
        return
    if command == "env repair" and (ctx.params.get("name") or ctx.params.get("shell") is not None):
        return
    if command == "apt profile apps" and not values.get("changed"):
        return
    base = ctx.obj.user_home or ctx.obj.home
    try:
        if command == "env detach":
            send("detach")
        elif command == "env switch":
            send("source", values["path"])
        elif command == "env drop":
            from .env_cli import _current_path
            if path := _current_path(base):
                send("source", path)
        elif command == "env repair" and not ctx.params.get("name") and ctx.params.get("shell") is None:
            send("hook", str(base.root))
        elif command == "apt profile apps" and values.get("changed"):
            from .env_entries import read_selection
            if read_selection(base) == "apt":
                _apt(base, values, only_current=True)
        elif apt_change:
            _apt(base, values)
        elif command == "profile use":
            send("source", values["activation"])
    except (OSError, ValueError, ForgeError) as exc:
        print(f"jforge: Command succeeded. Could not pass the shell activation: {exc}", file=sys.stderr)


def _apt(base, values, *, only_current=False):
    activation = values.get("environment_activation")
    if not activation:
        from .environment_registry import show
        activation = show(base, "apt", strict=True)["activation"]
    if only_current and os.environ.get("_JFORGE_SHELL_ENTRY") != activation:
        return
    send("source", activation)


HOOK = r'''# Add eval "$(command jforge apt shell-init)" to your shell startup file.
jforge() {
    local _jf_file _jf_status _jf_action _jf_value _jf_hook
    case $- in
        *i*) ;;
        *) command jforge "$@"; return $? ;;
    esac
    if ! _jf_file=$(mktemp "${TMPDIR:-/tmp}/jforge-shell.XXXXXXXX"); then
        printf '%s\n' 'jforge: Could not prepare shell activation.' >&2
        command jforge "$@"
        return $?
    fi
    if _JFORGE_SHELL_HANDOFF=1 _JFORGE_SHELL_ENTRY="${_JFORGE_ACTIVE_APT_ENTRY-}" command jforge "$@" 9>"$_jf_file"; then
        _jf_status=0
        while IFS= read -r -d '' _jf_action && IFS= read -r -d '' _jf_value; do
            case "$_jf_action" in
                source|build)
                    if source "$_jf_value" >&2; then
                        if [ "$_jf_action" = build ]; then
                            printf '%s\n' 'jforge: Build environment activated.' >&2
                        fi
                    else
                        printf 'jforge: Command succeeded. Could not activate %s\n' "$_jf_value" >&2
                    fi
                    ;;
                detach)
                    if typeset -f _jforge_env_restore >/dev/null 2>&1; then
                        _jforge_env_restore || printf '%s\n' 'jforge: Could not unload the selected environment.' >&2
                    elif typeset -f _jforge_apt_restore >/dev/null 2>&1; then
                        _jforge_apt_restore || printf '%s\n' 'jforge: Could not unload the selected environment.' >&2
                    fi
                    ;;
                hook)
                    _jf_hook=$(command jforge --home "$_jf_value" apt shell-init) || _jf_hook=''
                    if [ -z "$_jf_hook" ] || ! eval "$_jf_hook"; then
                        printf '%s\n' 'jforge: Could not refresh the shell hook.' >&2
                    fi
                    ;;
                *) printf '%s\n' 'jforge: Ignored an unknown shell activation action.' >&2 ;;
            esac
        done < "$_jf_file"
    else
        _jf_status=$?
    fi
    command rm -- "$_jf_file" 2>/dev/null || :
    return "$_jf_status"
}
_jforge_env_initial=$(command jforge__JFORGE_HOME_ARGS__ env current --path 2>/dev/null) || _jforge_env_initial=''
if [ -n "$_jforge_env_initial" ] && ! source "$_jforge_env_initial" >&2; then
    printf '%s\n' 'jforge: Could not load the selected environment.' >&2
fi
unset _jforge_env_initial
export _JFORGE_HOOK_LOADED=1
'''
