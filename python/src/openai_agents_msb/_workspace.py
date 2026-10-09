"""The host side of `/workspace`.

`/workspace` is a host directory bind-mounted into the VM, so outputs are read
from the host and survive the VM dying. Callers must stop the VM before reading
outputs, so the guest can't swap files for symlinks mid-read.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from .core import (
    FileOutput,
    WorkspaceContext,
    WorkspaceInput,
    assert_safe_filename,
    normalize_input,
)
from .core.types import input_name

__all__ = [
    "GUEST_WORKSPACE",
    "WorkspaceSnapshot",
    "assert_input_names",
    "clear_workspace_dir",
    "create_workspace_dir",
    "emit_file_outputs",
    "resolve_workspace_root",
    "seed_input_files",
    "session_dir",
    "snapshot_workspace",
]

GUEST_WORKSPACE = "/workspace"

# Files of 2 GiB or more are reported by path only, never read into memory.
MAX_BUFFER_BYTES = 2**31 - 1

_HASH_CHUNK = 1024 * 1024

T = TypeVar("T")


@dataclass(frozen=True)
class _Fingerprint:
    size: int
    mtime_ns: int
    digest: str


WorkspaceSnapshot = dict[str, _Fingerprint]
"""Fingerprints keyed by path relative to the workspace root."""


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    """Streams the file so hashing never loads it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_workspace_root(workspace_root: str | os.PathLike[str] | None = None) -> Path:
    """Directory holding one folder per persistent session."""
    if workspace_root is not None:
        return Path(workspace_root).resolve()
    return Path.home() / ".mst" / "workspaces"


def session_dir(name: str, workspace_root: str | os.PathLike[str] | None = None) -> Path:
    return resolve_workspace_root(workspace_root) / name


async def create_workspace_dir(
    name: str,
    persistent: bool,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Persistent sessions keep a stable directory per sandbox name; everything else
    gets a temp directory (under `<workspace_root>/.tmp` when a root is given).

    Real paths only: microsandbox can't bind a path that runs through a symlink
    (macOS /var -> /private/var).
    """
    return await asyncio.to_thread(_create_workspace_dir, name, persistent, workspace_root)


def _create_workspace_dir(
    name: str,
    persistent: bool,
    workspace_root: str | os.PathLike[str] | None,
) -> Path:
    if not persistent:
        parent = (
            resolve_workspace_root(workspace_root) / ".tmp"
            if workspace_root is not None
            else Path(tempfile.gettempdir())
        )
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="mst-ws-", dir=parent)).resolve()
    directory = session_dir(name, workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


async def clear_workspace_dir(directory: Path) -> None:
    await asyncio.to_thread(_clear_workspace_dir, directory)


def _clear_workspace_dir(directory: Path) -> None:
    import shutil

    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def assert_input_names(inputs: Sequence[WorkspaceInput]) -> None:
    seen: set[str] = set()
    for f in inputs:
        name = input_name(f)
        assert_safe_filename(name)
        if name in seen:
            raise ValueError(f"[mst] Duplicate input file name: {name!r}")
        seen.add(name)


async def seed_input_files(
    directory: Path, inputs: Sequence[WorkspaceInput]
) -> WorkspaceSnapshot:
    return await asyncio.to_thread(_seed_input_files, directory, inputs)


def _seed_input_files(directory: Path, inputs: Sequence[WorkspaceInput]) -> WorkspaceSnapshot:
    snapshot: WorkspaceSnapshot = {}
    for raw in inputs:
        f = normalize_input(raw)
        target = directory / f.name
        target.write_bytes(f.data)
        stat = target.stat()
        snapshot[f.name] = _Fingerprint(
            size=len(f.data), mtime_ns=stat.st_mtime_ns, digest=_hash_bytes(f.data)
        )
    return snapshot


def _walk(directory: Path) -> tuple[list[str], list[str]]:
    """Lists entries without following symlinks; only regular files count as outputs.

    Returns `(regular_files, all_entries)`, both as paths relative to `directory`.
    """
    files: list[str] = []
    everything: list[str] = []

    def visit(rel: str) -> None:
        with os.scandir(directory / rel if rel else directory) as entries:
            for entry in entries:
                rel_path = f"{rel}/{entry.name}" if rel else entry.name
                everything.append(rel_path)
                if entry.is_dir(follow_symlinks=False):
                    visit(rel_path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(rel_path)

    visit("")
    return files, everything


async def snapshot_workspace(directory: Path) -> WorkspaceSnapshot:
    return await asyncio.to_thread(_snapshot_workspace, directory)


def _snapshot_workspace(directory: Path) -> WorkspaceSnapshot:
    snapshot: WorkspaceSnapshot = {}
    files, _ = _walk(directory)
    for rel in files:
        path = directory / rel
        stat = path.stat()
        snapshot[rel] = _Fingerprint(
            size=stat.st_size, mtime_ns=stat.st_mtime_ns, digest=_hash_file(path)
        )
    return snapshot


async def _call(callback: Callable[[T], Any | Awaitable[Any]], value: T) -> None:
    """Callbacks may be sync or async."""
    result = callback(value)
    if inspect.isawaitable(result):
        await result


async def emit_file_outputs(
    directory: Path,
    ws: WorkspaceContext | None,
    input_snapshot: WorkspaceSnapshot,
) -> None:
    """Report every new or changed regular file in the workspace."""
    files, everything = await asyncio.to_thread(_walk, directory)

    # A failing snapshot callback (UI) must not drop the file outputs.
    try:
        if ws is not None and ws.on_workspace_snapshot is not None:
            paths = sorted(f"{GUEST_WORKSPACE}/{rel}" for rel in everything)
            await _call(ws.on_workspace_snapshot, paths)
    finally:
        if ws is not None and ws.on_file_output is not None:
            for rel in files:
                output = await asyncio.to_thread(
                    _read_output, directory, rel, input_snapshot
                )
                if output is not None:
                    await _call(ws.on_file_output, output)


def _read_output(
    directory: Path, rel: str, input_snapshot: WorkspaceSnapshot
) -> FileOutput | None:
    path = directory / rel
    stat = path.stat()
    original = input_snapshot.get(rel)

    if original is not None:
        # Fast path: skip files whose size and mtime haven't changed.
        if stat.st_size == original.size and stat.st_mtime_ns == original.mtime_ns:
            return None
        # Slow path: hash to confirm the change.
        if stat.st_size == original.size and _hash_file(path) == original.digest:
            return None

    return FileOutput(
        file_name=rel,
        size=stat.st_size,
        path=str(path),
        data=path.read_bytes() if stat.st_size <= MAX_BUFFER_BYTES else None,
    )
