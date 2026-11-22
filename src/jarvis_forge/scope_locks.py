"""Lifecycle leases live outside directories that profile remove must delete."""

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os

from .storage import ForgeError

_HELD = ContextVar("forge_scope_leases", default={})


def path(scope):
    owner = scope.root.parent if scope.root.name == "tools" and scope.root.parent.parent.name == "profiles" else scope.root
    if owner.parent.name != "profiles":
        return None
    return owner.parent.parent / "locks/scopes" / ("profiles-" + owner.name + ".lock")


def initialize(scope):
    file = path(scope)
    if file:
        file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)


@contextmanager
def lease(scope, *, exclusive=False, readonly=False):
    file = path(scope)
    key = str(file)
    if file is None or key in _HELD.get():
        if exclusive and _HELD.get().get(key) is False:
            raise ForgeError("Cannot remove a scope during its current operation.")
        yield
        return
    if not readonly:
        initialize(scope)
    descriptor = os.open(file, (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r" if readonly else "r+") as stream:
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(f"{scope.root.name} is busy; its current operation must finish first.") from exc
        token = _HELD.set({**_HELD.get(), key: exclusive})
        try:
            if not scope.root.is_dir():
                raise ForgeError(f"Profile is no longer available: {scope.root}")
            yield
        finally:
            _HELD.reset(token)
            fcntl.flock(stream, fcntl.LOCK_UN)
