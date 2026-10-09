"""Streaming: teardown runs however the stream ends."""

from __future__ import annotations

import asyncio

import pytest
from agents import Agent

from openai_agents_msb import (
    FileOutput,
    SandboxSetupError,
    WorkspaceContext,
    run_streamed,
    sandbox_config,
    sandbox_write_file,
)

from .conftest import SandboxRecorder, agent_calling, calls_tool, says

PY = {"image": "python:3.12-slim", "interpreter": "python3"}


def writing_agent(content: str = "# report") -> Agent[object]:
    return agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/out.md", "content": content}),
        says("done"),
    )


async def test_streaming_yields_events_and_tears_down(sandboxes: SandboxRecorder) -> None:
    outputs: list[FileOutput] = []
    result = run_streamed(
        writing_agent(), "go", WorkspaceContext(on_file_output=outputs.append)
    )
    events = [event async for event in result.stream_events()]

    assert events
    assert result.final_output == "done"
    assert [(o.file_name, o.data) for o in outputs] == [("out.md", b"# report")]
    assert sandboxes.only.stopped == 1
    assert not sandboxes.only.host_dir.exists()


async def test_wait_completed_tears_down_without_iterating(
    sandboxes: SandboxRecorder,
) -> None:
    outputs: list[FileOutput] = []
    result = run_streamed(
        writing_agent(), "go", WorkspaceContext(on_file_output=outputs.append)
    )
    await result.wait_completed()

    assert [o.file_name for o in outputs] == ["out.md"]
    assert sandboxes.only.stopped == 1


async def test_closing_early_stops_the_run_and_leaks_nothing(
    sandboxes: SandboxRecorder,
) -> None:
    """aclose() means stop now, so the pending tool call never boots a VM."""
    result = run_streamed(writing_agent(), "go")
    async for _ in result.stream_events():
        break
    await result.aclose()

    assert sandboxes.created == []
    assert result.is_complete


async def test_breaking_out_after_a_tool_call_tears_the_vm_down(
    sandboxes: SandboxRecorder,
) -> None:
    result = run_streamed(writing_agent(), "go")
    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and event.name == "tool_output":
            break
    await result.aclose()

    assert sandboxes.only.stopped == 1
    assert not sandboxes.only.host_dir.exists()


async def test_the_run_loop_tears_down_an_abandoned_stream(
    sandboxes: SandboxRecorder,
) -> None:
    """An async generator is not finalized promptly, so the run loop is the backstop."""
    result = run_streamed(writing_agent(), "go")
    async for _ in result.stream_events():
        break

    # Nothing but the backstop is holding the VM now.
    assert result._mst_teardown is not None
    await asyncio.shield(result._mst_teardown)

    assert sandboxes.only.stopped == 1
    assert not sandboxes.only.host_dir.exists()


async def test_cancelling_the_consumer_tears_down(sandboxes: SandboxRecorder) -> None:
    result = run_streamed(writing_agent(), "go")

    async def consume() -> None:
        async for _ in result.stream_events():
            await asyncio.sleep(0.05)

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await result.aclose()
    assert all(not sb.host_dir.exists() for sb in sandboxes.created)
    assert all(sb.stopped == 1 for sb in sandboxes.created)


async def test_async_with_closes_the_sandbox(sandboxes: SandboxRecorder) -> None:
    outputs: list[FileOutput] = []
    async with run_streamed(
        writing_agent(), "go", WorkspaceContext(on_file_output=outputs.append)
    ) as result:
        async for _ in result.stream_events():
            pass

    assert [o.file_name for o in outputs] == ["out.md"]
    assert sandboxes.only.stopped == 1


async def test_chat_only_streams_boot_nothing(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling([sandbox_config(**PY)], says("hello"))  # type: ignore[arg-type]
    result = run_streamed(agent, "hi")
    await result.wait_completed()

    assert sandboxes.created == []
    assert result.final_output == "hello"


async def test_teardown_happens_once(sandboxes: SandboxRecorder) -> None:
    outputs: list[FileOutput] = []
    result = run_streamed(
        writing_agent(), "go", WorkspaceContext(on_file_output=outputs.append)
    )
    await result.wait_completed()
    await result.wait_completed()

    assert sandboxes.only.stopped == 1
    assert len(outputs) == 1


async def test_config_errors_raise_before_streaming_starts(
    sandboxes: SandboxRecorder,
) -> None:
    agent = agent_calling(
        [
            sandbox_config(image="python:3.12-slim", interpreter="python3"),
            sandbox_config(image="ubuntu", interpreter="bash"),
        ],
        says("never reached"),
    )
    with pytest.raises(ValueError, match="identical SandboxConfig"):
        run_streamed(agent, "go")
    assert sandboxes.created == []


async def test_setup_failures_surface_unwrapped(
    sandboxes: SandboxRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openai_agents_msb._run as run_module

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("no hypervisor")

    monkeypatch.setattr(run_module, "_create_sandbox", boom)
    result = run_streamed(writing_agent(), "go")
    with pytest.raises(SandboxSetupError, match="no hypervisor"):
        await result.wait_completed()


async def test_concurrent_streams_stay_isolated(sandboxes: SandboxRecorder) -> None:
    first: list[FileOutput] = []
    second: list[FileOutput] = []

    a = run_streamed(writing_agent("one"), "a", WorkspaceContext(on_file_output=first.append))
    b = run_streamed(writing_agent("two"), "b", WorkspaceContext(on_file_output=second.append))
    await asyncio.gather(a.wait_completed(), b.wait_completed())

    assert [o.data for o in first] == [b"one"]
    assert [o.data for o in second] == [b"two"]
    assert len({sb.name for sb in sandboxes.created}) == 2
