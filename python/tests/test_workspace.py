"""Seeding `/workspace`, and deciding which files come back out."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openai_agents_msb import FileOutput, InputFile, WorkspaceContext
from openai_agents_msb._workspace import (
    WorkspaceSnapshot,
    assert_input_names,
    clear_workspace_dir,
    create_workspace_dir,
    emit_file_outputs,
    resolve_workspace_root,
    seed_input_files,
    session_dir,
    snapshot_workspace,
)


async def collect(
    directory: Path, ws: WorkspaceContext, snapshot: WorkspaceSnapshot
) -> list[FileOutput]:
    outputs: list[FileOutput] = []
    ws.on_file_output = outputs.append
    await emit_file_outputs(directory, ws, snapshot)
    return outputs


# -- seeding ---------------------------------------------------------------------------


async def test_input_files_land_in_the_workspace(tmp_path: Path) -> None:
    await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    assert (tmp_path / "a.txt").read_bytes() == b"hello"


async def test_paths_are_seeded_by_basename(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"1,2,3")
    target = tmp_path / "ws"
    target.mkdir()
    await seed_input_files(target, [source])
    assert (target / "source.csv").read_bytes() == b"1,2,3"


@pytest.mark.parametrize("name", ["../escape.txt", "nested/file.txt", "", "a\\b.txt"])
def test_unsafe_input_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid filename"):
        assert_input_names([InputFile(name, b"")])


def test_duplicate_input_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate input file name"):
        assert_input_names([InputFile("a.txt", b"1"), InputFile("a.txt", b"2")])


# -- diffing ---------------------------------------------------------------------------


async def test_unchanged_inputs_are_not_reported(tmp_path: Path) -> None:
    snapshot = await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    assert await collect(tmp_path, WorkspaceContext(), snapshot) == []


async def test_new_files_are_reported_with_their_bytes(tmp_path: Path) -> None:
    snapshot = await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    (tmp_path / "b.txt").write_bytes(b"fresh")
    outputs = await collect(tmp_path, WorkspaceContext(), snapshot)
    assert [(o.file_name, o.data, o.size) for o in outputs] == [("b.txt", b"fresh", 5)]


async def test_changed_inputs_are_reported(tmp_path: Path) -> None:
    snapshot = await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    (tmp_path / "a.txt").write_bytes(b"goodbye")
    outputs = await collect(tmp_path, WorkspaceContext(), snapshot)
    assert [o.file_name for o in outputs] == ["a.txt"]


async def test_same_size_rewrite_is_caught_by_the_hash(tmp_path: Path) -> None:
    """A new mtime sends the file down the hashing path, which sees the change."""
    snapshot = await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    path = tmp_path / "a.txt"
    path.write_bytes(b"world")
    _bump_mtime(path)
    outputs = await collect(tmp_path, WorkspaceContext(), snapshot)
    assert [o.file_name for o in outputs] == ["a.txt"]


async def test_touching_a_file_without_changing_it_reports_nothing(tmp_path: Path) -> None:
    """The hash is what decides, so a bumped mtime alone is not a change."""
    snapshot = await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    _bump_mtime(tmp_path / "a.txt")
    assert await collect(tmp_path, WorkspaceContext(), snapshot) == []


async def test_rewrite_that_preserves_size_and_mtime_is_skipped(tmp_path: Path) -> None:
    """The fast path trusts (size, mtime); matching TS, a restored mtime hides the write."""
    snapshot = await seed_input_files(tmp_path, [InputFile("a.txt", b"hello")])
    path = tmp_path / "a.txt"
    stat = path.stat()
    path.write_bytes(b"world")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert await collect(tmp_path, WorkspaceContext(), snapshot) == []


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


async def test_nested_files_are_reported_by_relative_path(tmp_path: Path) -> None:
    (tmp_path / "reports" / "q1").mkdir(parents=True)
    (tmp_path / "reports" / "q1" / "summary.md").write_bytes(b"#")
    outputs = await collect(tmp_path, WorkspaceContext(), {})
    assert [o.file_name for o in outputs] == ["reports/q1/summary.md"]


async def test_symlinks_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_bytes(b"data")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    (tmp_path / "escape").symlink_to("/etc")
    outputs = await collect(tmp_path, WorkspaceContext(), {})
    assert [o.file_name for o in outputs] == ["real.txt"]


async def test_fifos_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_bytes(b"data")
    os.mkfifo(tmp_path / "pipe")
    outputs = await collect(tmp_path, WorkspaceContext(), {})
    assert [o.file_name for o in outputs] == ["ok.txt"]


async def test_odd_names_survive(tmp_path: Path) -> None:
    for name in ["a b.txt", "ünïcode.txt", "with'quote.txt", "dot.tar.gz"]:
        (tmp_path / name).write_bytes(b"x")
    outputs = await collect(tmp_path, WorkspaceContext(), {})
    assert len(outputs) == 4


async def test_host_path_is_usable_during_the_callback(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"payload")
    seen: list[bytes] = []
    ws = WorkspaceContext(on_file_output=lambda f: seen.append(Path(f.path).read_bytes()))
    await emit_file_outputs(tmp_path, ws, {})
    assert seen == [b"payload"]


# -- callbacks -------------------------------------------------------------------------


async def test_async_callbacks_are_awaited(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    seen: list[str] = []

    async def on_output(f: FileOutput) -> None:
        seen.append(f.file_name)

    await emit_file_outputs(tmp_path, WorkspaceContext(on_file_output=on_output), {})
    assert seen == ["a.txt"]


async def test_snapshot_lists_every_entry_as_a_guest_path(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "deep.txt").write_bytes(b"x")
    (tmp_path / "top.txt").write_bytes(b"x")
    seen: list[list[str]] = []
    ws = WorkspaceContext(on_workspace_snapshot=seen.append)
    await emit_file_outputs(tmp_path, ws, {})
    assert seen == [["/workspace/dir", "/workspace/dir/deep.txt", "/workspace/top.txt"]]


async def test_failing_snapshot_callback_does_not_drop_outputs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    outputs: list[FileOutput] = []

    def explode(_: list[str]) -> None:
        raise RuntimeError("UI blew up")

    ws = WorkspaceContext(on_workspace_snapshot=explode, on_file_output=outputs.append)
    with pytest.raises(RuntimeError, match="UI blew up"):
        await emit_file_outputs(tmp_path, ws, {})
    assert [o.file_name for o in outputs] == ["a.txt"]


# -- directories -----------------------------------------------------------------------


def test_workspace_root_defaults_under_home() -> None:
    assert resolve_workspace_root() == Path.home() / ".mst" / "workspaces"


def test_session_dir_is_one_folder_per_name(tmp_path: Path) -> None:
    assert session_dir("case-42", tmp_path) == tmp_path / "case-42"


async def test_temp_workspaces_go_under_the_root_when_given(tmp_path: Path) -> None:
    directory = await create_workspace_dir("mst-x", persistent=False, workspace_root=tmp_path)
    assert directory.parent == (tmp_path / ".tmp").resolve()
    assert directory.name.startswith("mst-ws-")


async def test_persistent_workspaces_are_stable(tmp_path: Path) -> None:
    first = await create_workspace_dir("case-42", persistent=True, workspace_root=tmp_path)
    second = await create_workspace_dir("case-42", persistent=True, workspace_root=tmp_path)
    assert first == second == (tmp_path / "case-42").resolve()


async def test_workspace_dirs_are_real_paths(tmp_path: Path) -> None:
    """microsandbox can't bind a path that runs through a symlink."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    directory = await create_workspace_dir("case-42", persistent=True, workspace_root=link)
    assert directory == (real / "case-42").resolve()


async def test_clearing_removes_files_and_trees(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "a.txt")
    await clear_workspace_dir(tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_snapshot_fingerprints_existing_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    snapshot = await snapshot_workspace(tmp_path)
    assert set(snapshot) == {"a.txt"}
    # A follow-up turn sees no change.
    assert await collect(tmp_path, WorkspaceContext(), snapshot) == []
