"""Persistent sessions: one VM and one workspace per conversation."""

from __future__ import annotations

import asyncio
import os
import shutil
from asyncio import AbstractEventLoop
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from weakref import WeakKeyDictionary

from microsandbox import Sandbox, SandboxNotFoundError, SandboxStatus

from .core import assert_session_name
from ._workspace import resolve_workspace_root, session_dir

__all__ = ["SessionInfo", "end_session", "list_sessions"]

SandboxState = Literal["running", "stopped", "missing"]


@dataclass(frozen=True)
class SessionInfo:
    name: str
    workspace_dir: str
    """Host directory mounted at /workspace."""
    sandbox: SandboxState
    """State of the session's VM; 'missing' if it was removed."""
    last_used: datetime
    """End of the most recent `run()` on this session."""


# An asyncio.Lock belongs to the loop that first awaits it, so the locks are kept
# per loop: run_sync() and separate asyncio.run() calls each get their own, and
# they are collected with their loop.
_session_locks: WeakKeyDictionary[AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


async def acquire_session(name: str) -> Callable[[], None]:
    """Runs sharing a sandbox name take turns, so one can't replace the other's VM mid-run."""
    locks = _session_locks.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault(name, asyncio.Lock())
    await lock.acquire()

    def release() -> None:
        lock.release()
        if not lock.locked() and locks.get(name) is lock:
            del locks[name]

    return release


async def _sandbox_state(name: str) -> SandboxState:
    try:
        handle = await Sandbox.get(name)
    except SandboxNotFoundError:
        return "missing"
    return "running" if handle.status == SandboxStatus.RUNNING else "stopped"


async def list_sessions(
    *, workspace_root: str | os.PathLike[str] | None = None
) -> list[SessionInfo]:
    """Persistent sessions under `workspace_root`, most recently used first.

    `workspace_root` must match the one passed to `WorkspaceContext`.
    """
    root = resolve_workspace_root(workspace_root)
    try:
        entries = await asyncio.to_thread(lambda: sorted(root.iterdir()))
    except FileNotFoundError:
        return []

    sessions: list[SessionInfo] = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        workspace_dir = session_dir(entry.name, workspace_root)
        stat = workspace_dir.stat()
        sessions.append(
            SessionInfo(
                name=entry.name,
                workspace_dir=str(workspace_dir),
                sandbox=await _sandbox_state(entry.name),
                last_used=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    sessions.sort(key=lambda s: s.last_used, reverse=True)
    return sessions


async def end_session(
    name: str,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    keep_files: bool = False,
) -> None:
    """Delete a persistent session's VM and, unless `keep_files`, its workspace files.

    Waits for queued `run()` calls on the session to finish first.
    """
    assert_session_name(name)
    release = await acquire_session(name)
    try:
        try:
            await Sandbox.remove(name)
        except SandboxNotFoundError:
            pass
        if not keep_files:
            directory = session_dir(name, workspace_root)
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
    finally:
        release()


def touch_session_dir(directory: Path) -> None:
    """`list_sessions()` reports this as `last_used`."""
    try:
        os.utime(directory, None)
    except OSError:
        pass
