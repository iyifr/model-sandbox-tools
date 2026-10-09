"""Persistent sessions: one VM and one workspace per conversation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agents import Agent
from microsandbox import Sandbox, SandboxNotFoundError

from openai_agents_msb import (
    FileOutput,
    InputFile,
    SessionNotFoundError,
    WorkspaceContext,
    end_session,
    list_sessions,
    run,
    sandbox_config,
    sandbox_write_file,
)
from openai_agents_msb._session import acquire_session

from .conftest import SandboxRecorder, agent_calling, calls_tool, says

PY = {"image": "python:3.12-slim", "interpreter": "python3"}


def writer(content: str = "x", *, persist: bool = True) -> Agent[Any]:
    return agent_calling(
        [sandbox_config(**PY, persist=persist), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/out.md", "content": content}),
        says("done"),
    )


@pytest.fixture
def persistent_sandboxes(
    sandboxes: SandboxRecorder, monkeypatch: pytest.MonkeyPatch
) -> SandboxRecorder:
    """Let `Sandbox.start` and `Sandbox.get` find whatever this test created."""

    async def fake_get(name: str) -> object:
        for sb in sandboxes.created:
            if sb.name == name and name not in sandboxes.removed:
                return sb
        raise SandboxNotFoundError(f"no sandbox named {name}")

    async def fake_start(name: str, **_: object) -> object:
        return await fake_get(name)

    monkeypatch.setattr(Sandbox, "get", fake_get)
    monkeypatch.setattr(Sandbox, "start", fake_start)
    return sandboxes


# -- workspace location ----------------------------------------------------------------


async def test_a_named_session_keeps_a_stable_workspace(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(), "go", ws)

    assert persistent_sandboxes.only.host_dir == (workspace_root / "case-42").resolve()
    # The workspace survives the run.
    assert (workspace_root / "case-42" / "out.md").read_bytes() == b"x"


async def test_a_persistent_vm_is_not_removed(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(), "go", ws)

    assert persistent_sandboxes.only.stopped == 1
    assert persistent_sandboxes.removed == []


async def test_a_name_without_persist_is_still_ephemeral(
    sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(persist=False), "go", ws)

    assert sandboxes.removed == ["case-42"]
    assert not (workspace_root / "case-42").exists()


@pytest.mark.parametrize("name", ["../escape", ".hidden", "a/b", ""])
async def test_unsafe_session_names_are_rejected(
    sandboxes: SandboxRecorder, workspace_root: Path, name: str
) -> None:
    ws = WorkspaceContext(sandbox_name=name, workspace_root=workspace_root)
    with pytest.raises(ValueError, match="Invalid"):
        await run(writer(), "go", ws)


# -- follow-up turns -------------------------------------------------------------------


async def test_a_follow_up_turn_reuses_the_vm(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    first = WorkspaceContext(
        sandbox_name="case-42",
        workspace_root=workspace_root,
        input_files=[InputFile("brief.md", b"original")],
    )
    await run(writer("first"), "go", first)

    second = WorkspaceContext(
        sandbox_name="case-42", workspace_root=workspace_root, skip_input_seed=True
    )
    await run(writer("second"), "again", second)

    assert len(persistent_sandboxes.created) == 1
    assert (workspace_root / "case-42" / "brief.md").read_bytes() == b"original"
    assert (workspace_root / "case-42" / "out.md").read_bytes() == b"second"


async def test_a_follow_up_turn_only_reports_what_changed(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    first = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer("same"), "go", first)

    outputs: list[FileOutput] = []
    second = WorkspaceContext(
        sandbox_name="case-42",
        workspace_root=workspace_root,
        skip_input_seed=True,
        on_file_output=outputs.append,
    )
    await run(writer("same"), "again", second)

    # The file was rewritten with identical content, so nothing is reported.
    assert outputs == []


async def test_a_follow_up_on_a_deleted_session_raises(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(), "go", ws)
    await end_session("case-42", workspace_root=workspace_root)

    follow_up = WorkspaceContext(
        sandbox_name="case-42", workspace_root=workspace_root, skip_input_seed=True
    )
    with pytest.raises(SessionNotFoundError, match="workspace directory is missing"):
        await run(writer(), "again", follow_up)


async def test_a_follow_up_with_files_but_no_vm_raises(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(), "go", ws)
    await end_session("case-42", workspace_root=workspace_root, keep_files=True)

    follow_up = WorkspaceContext(
        sandbox_name="case-42", workspace_root=workspace_root, skip_input_seed=True
    )
    with pytest.raises(SessionNotFoundError, match="sandbox was removed"):
        await run(writer(), "again", follow_up)


# -- listing and ending ----------------------------------------------------------------


async def test_list_sessions_is_empty_for_a_fresh_root(workspace_root: Path) -> None:
    assert await list_sessions(workspace_root=workspace_root) == []


async def test_list_sessions_is_empty_for_a_missing_root(tmp_path: Path) -> None:
    assert await list_sessions(workspace_root=tmp_path / "nope") == []


async def test_list_sessions_reports_each_session(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    for name in ["case-1", "case-2"]:
        ws = WorkspaceContext(sandbox_name=name, workspace_root=workspace_root)
        await run(writer(), "go", ws)

    sessions = await list_sessions(workspace_root=workspace_root)
    assert {s.name for s in sessions} == {"case-1", "case-2"}
    assert all(s.sandbox == "stopped" for s in sessions)
    assert all(Path(s.workspace_dir).exists() for s in sessions)


async def test_list_sessions_is_most_recent_first(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    for name in ["old", "new"]:
        ws = WorkspaceContext(sandbox_name=name, workspace_root=workspace_root)
        await run(writer(), "go", ws)
        await asyncio.sleep(0.01)
    sessions = await list_sessions(workspace_root=workspace_root)
    assert [s.name for s in sessions] == ["new", "old"]


async def test_list_sessions_skips_the_temp_directory(
    sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name=None, workspace_root=workspace_root)
    await run(writer(persist=False), "go", ws)
    assert (workspace_root / ".tmp").exists()
    assert await list_sessions(workspace_root=workspace_root) == []


async def test_end_session_deletes_the_files(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(), "go", ws)

    await end_session("case-42", workspace_root=workspace_root)
    assert not (workspace_root / "case-42").exists()
    assert await list_sessions(workspace_root=workspace_root) == []


async def test_end_session_can_keep_the_files(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    await run(writer(), "go", ws)

    await end_session("case-42", workspace_root=workspace_root, keep_files=True)
    assert (workspace_root / "case-42" / "out.md").exists()
    sessions = await list_sessions(workspace_root=workspace_root)
    assert [(s.name, s.sandbox) for s in sessions] == [("case-42", "missing")]


async def test_end_session_on_an_unknown_name_is_quiet(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    await end_session("never-existed", workspace_root=workspace_root)


# -- queueing --------------------------------------------------------------------------


async def test_runs_on_one_session_are_queued(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    def ws(on_output: object = None) -> WorkspaceContext:
        return WorkspaceContext(
            sandbox_name="case-42",
            workspace_root=workspace_root,
            on_file_output=on_output,  # type: ignore[arg-type]
        )

    await asyncio.gather(
        run(writer("one"), "a", ws()),
        run(writer("two"), "b", ws()),
    )
    # Each turn replaces the session's VM, so what matters is that the two runs
    # never overlapped and never raced for the same workspace.
    assert persistent_sandboxes.peak_live == 1
    assert {sb.name for sb in persistent_sandboxes.created} == {"case-42"}
    assert len({sb.host_dir for sb in persistent_sandboxes.created}) == 1


async def test_the_session_lock_is_released_and_reusable() -> None:
    release = await acquire_session("case-42")
    waiter = asyncio.ensure_future(acquire_session("case-42"))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    release()
    second_release = await waiter
    second_release()


async def test_the_session_lock_is_released_when_a_run_fails(
    persistent_sandboxes: SandboxRecorder, workspace_root: Path
) -> None:
    broken = agent_calling(
        [sandbox_config(**PY, persist=True)],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print(1)"}),
        # No second model step, so the run fails mid-flight.
    )
    ws = WorkspaceContext(sandbox_name="case-42", workspace_root=workspace_root)
    with pytest.raises(Exception):
        await run(broken, "go", ws)

    # The lock is free, so the next turn is not deadlocked.
    await run(writer(), "again", ws)
