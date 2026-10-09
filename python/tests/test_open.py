"""Agent graphs: handoffs, as_tool() agents, and workspaces at scale."""

from __future__ import annotations

from typing import Any

from agents import Agent

from openai_agents_msb import (
    FileOutput,
    InputFile,
    WorkspaceContext,
    run,
    sandbox_config,
    sandbox_exec,
    sandbox_write_file,
)
from openai_agents_msb._discover import DEFAULT_SANDBOX_CONFIG, discover_sandbox_config

from .conftest import SandboxRecorder, agent_calling, calls_tool, says, scripted

PY = {"image": "python:3.12-slim", "interpreter": "python3"}


# -- discovery -------------------------------------------------------------------------


def test_no_sandbox_tools_is_reported() -> None:
    agent = Agent(name="plain", instructions="chat", model=scripted(says("hi")))
    discovered = discover_sandbox_config(agent)
    assert discovered.config is None
    assert discovered.has_tools is False


def test_tools_without_a_config_fall_back_to_the_default() -> None:
    agent = agent_calling([sandbox_exec()], says("hi"))
    discovered = discover_sandbox_config(agent)
    assert discovered.config is None
    assert discovered.has_tools is True
    assert DEFAULT_SANDBOX_CONFIG.image == "python:3.12-slim"
    assert DEFAULT_SANDBOX_CONFIG.interpreter == "python3"


def test_a_config_is_found_through_handoffs() -> None:
    specialist = agent_calling([sandbox_config(image="ubuntu", interpreter="bash")], says("x"))
    agent = agent_calling([sandbox_exec()], says("x"), handoffs=[specialist])
    discovered = discover_sandbox_config(agent)
    assert discovered.config is not None
    assert discovered.config.image == "ubuntu"


def test_identical_configs_in_the_graph_are_accepted() -> None:
    specialist = agent_calling([sandbox_config(**PY)], says("x"))  # type: ignore[arg-type]
    agent = agent_calling([sandbox_config(**PY)], says("x"), handoffs=[specialist])  # type: ignore[arg-type]
    assert discover_sandbox_config(agent).config is not None


def test_a_handoff_cycle_terminates() -> None:
    a: Agent[Any] = agent_calling([sandbox_config(**PY)], says("x"), name="a")  # type: ignore[arg-type]
    b: Agent[Any] = agent_calling([sandbox_exec()], says("x"), name="b", handoffs=[a])
    a.handoffs = [b]
    assert discover_sandbox_config(a).config is not None


# -- sharing one VM --------------------------------------------------------------------


async def test_a_handoff_shares_the_run_s_vm(sandboxes: SandboxRecorder) -> None:
    specialist = Agent(
        name="writer",
        instructions="write the file",
        tools=[sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        model=scripted(
            calls_tool(
                "sandbox_write_file", {"path": "/workspace/b.md", "content": "b"}, call_id="c2"
            ),
            says("handed off and done"),
        ),
    )
    agent = Agent(
        name="router",
        instructions="hand off to the writer",
        tools=[sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        handoffs=[specialist],
        model=scripted(
            calls_tool(
                "sandbox_write_file", {"path": "/workspace/a.md", "content": "a"}, call_id="c1"
            ),
            [_handoff_call("writer")],
        ),
    )

    outputs: list[FileOutput] = []
    result = await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))

    assert result.final_output == "handed off and done"
    assert len(sandboxes.created) == 1
    assert {o.file_name for o in outputs} == {"a.md", "b.md"}


async def test_an_as_tool_agent_shares_the_run_s_vm(sandboxes: SandboxRecorder) -> None:
    """as_tool() agents can't be inspected, so they join whatever VM the run picked."""
    inner = Agent(
        name="helper",
        instructions="write the file",
        tools=[sandbox_write_file()],
        model=scripted(
            calls_tool(
                "sandbox_write_file", {"path": "/workspace/inner.md", "content": "i"}, call_id="c9"
            ),
            says("inner done"),
        ),
    )
    agent = Agent(
        name="outer",
        instructions="use the helper",
        tools=[sandbox_config(**PY), inner.as_tool("helper", "Delegate writing")],  # type: ignore[arg-type]
        model=scripted(
            calls_tool("helper", {"input": "write it"}, call_id="c1"),
            says("done"),
        ),
    )

    outputs: list[FileOutput] = []
    await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))

    assert len(sandboxes.created) == 1
    assert [o.file_name for o in outputs] == ["inner.md"]


# -- scale and odd names ---------------------------------------------------------------


async def test_many_input_files_round_trip(sandboxes: SandboxRecorder) -> None:
    files = [InputFile(f"file-{i:05d}.txt", f"{i}".encode()) for i in range(2000)]
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/new.md", "content": "n"}),
        says("done"),
    )
    await run(agent, "go", WorkspaceContext(input_files=files, on_file_output=outputs.append))

    # Only the new file is reported; the 2000 untouched inputs are not.
    assert [o.file_name for o in outputs] == ["new.md"]


async def test_odd_input_names_survive_the_round_trip(sandboxes: SandboxRecorder) -> None:
    names = ["a b.txt", "ünïcode.txt", "with'quote.txt", "dot.tar.gz", "UPPER.TXT"]
    snapshot: list[list[str]] = []
    agent = agent_calling([sandbox_config(**PY)], says("done"))  # type: ignore[arg-type]
    ws = WorkspaceContext(
        input_files=[InputFile(n, b"x") for n in names],
        on_workspace_snapshot=snapshot.append,
    )
    await run(agent, "go", ws)
    # No tool ran, so no VM booted and there is no workspace to report.
    assert sandboxes.created == []
    assert snapshot == []


def _handoff_call(agent_name: str) -> Any:
    from agents.testing.model import function_call

    return function_call(f"transfer_to_{agent_name}", {}, call_id="h1")
