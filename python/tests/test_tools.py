"""What the model sees, and what each tool does."""

from __future__ import annotations

from typing import Any

import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext

from openai_agents_msb import (
    WorkspaceContext,
    run,
    sandbox_config,
    sandbox_exec,
    sandbox_list_files,
    sandbox_read_file,
    sandbox_write_file,
)
from openai_agents_msb._discover import MST_SANDBOX_CONFIG_ATTR, MST_TOOL_ATTR

from .conftest import SandboxRecorder, agent_calling, calls_tool, says

PY = {"image": "python:3.12-slim", "interpreter": "python3"}


async def invoke(tool: FunctionTool, **args: Any) -> Any:
    import json

    payload = json.dumps(args)
    ctx: ToolContext[None] = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=payload
    )
    return await tool.on_invoke_tool(ctx, payload)


# -- the names and schemas the model sees ----------------------------------------------


def test_the_model_facing_tool_names_are_stable() -> None:
    """sandbox_config() is the developer-facing name; the model still sees sandbox_run."""
    assert sandbox_config(**PY).name == "sandbox_run"  # type: ignore[arg-type]
    assert sandbox_exec().name == "sandbox_exec"
    assert sandbox_read_file().name == "sandbox_read_file"
    assert sandbox_write_file().name == "sandbox_write_file"
    assert sandbox_list_files().name == "sandbox_list_files"


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (sandbox_config(**PY), {"script"}),  # type: ignore[arg-type]
        (sandbox_exec(), {"command"}),
        (sandbox_read_file(), {"path"}),
        (sandbox_write_file(), {"path", "content"}),
        (sandbox_list_files(), {"path"}),
    ],
)
def test_tool_parameters(tool: FunctionTool, params: set[str]) -> None:
    assert set(tool.params_json_schema["properties"]) == params


def test_parameters_are_described_for_the_model() -> None:
    schema = sandbox_write_file().params_json_schema
    assert "Absolute path" in schema["properties"]["path"]["description"]
    assert "Text content" in schema["properties"]["content"]["description"]


def test_every_tool_is_marked_for_discovery() -> None:
    tools = [
        sandbox_config(**PY),  # type: ignore[arg-type]
        sandbox_exec(),
        sandbox_read_file(),
        sandbox_write_file(),
        sandbox_list_files(),
    ]
    assert all(getattr(t, MST_TOOL_ATTR, False) for t in tools)
    # Only sandbox_config() carries the config.
    assert [getattr(t, MST_SANDBOX_CONFIG_ATTR, None) is not None for t in tools] == [
        True,
        False,
        False,
        False,
        False,
    ]


def test_sandbox_config_records_its_options() -> None:
    tool = sandbox_config(image="ubuntu", interpreter="bash", memory=512, cpus=2)
    config = getattr(tool, MST_SANDBOX_CONFIG_ATTR)
    assert (config.image, config.interpreter, config.memory, config.cpus) == (
        "ubuntu",
        "bash",
        512,
        2,
    )


# -- behaviour -------------------------------------------------------------------------


async def test_sandbox_run_writes_the_script_and_cleans_it_up(
    sandboxes: SandboxRecorder,
) -> None:
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print('hi')"}),
        says("done"),
    )
    await run(agent, "go")

    sb = sandboxes.only
    assert len(sb.writes) == 1 and sb.writes[0].startswith("/tmp/mst_script_")
    assert sb.removes == sb.writes
    assert sb.execs == [("python3", sb.writes)]


async def test_the_interpreter_comes_from_the_config(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(image="node:22", interpreter="node")],
        calls_tool("sandbox_run", {"script": "console.log(1)"}),
        says("done"),
    )
    await run(agent, "go")
    assert sandboxes.only.execs[0][0] == "node"


async def test_sandbox_exec_runs_through_a_shell(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_exec", {"command": "ls -la"}),
        says("done"),
    )
    await run(agent, "go")
    assert sandboxes.only.execs == [("/bin/sh", ["-c", "ls -la"])]


async def test_results_come_back_as_yaml(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_exec", {"command": "echo hi"}),
        says("done"),
    )
    result = await run(agent, "go")
    output = next(i for i in result.new_items if i.type == "tool_call_output_item")
    assert str(output.output).startswith("exit_code: 0\nstdout: |\n  ok")


async def test_write_then_read_round_trips(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file(), sandbox_read_file()],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_write_file",
            {"path": "/workspace/notes/memo.md", "content": "hello"},
            call_id="c1",
        ),
        calls_tool("sandbox_read_file", {"path": "/workspace/notes/memo.md"}, call_id="c2"),
        says("done"),
    )
    result = await run(agent, "go")
    outputs = [str(i.output) for i in result.new_items if i.type == "tool_call_output_item"]
    assert outputs == ["written /workspace/notes/memo.md (5 bytes)", "hello"]


async def test_write_file_rejects_unsafe_paths(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/../etc/x", "content": "x"}),
        says("done"),
    )
    result = await run(agent, "go")
    output = next(i for i in result.new_items if i.type == "tool_call_output_item")
    assert "invalid path" in str(output.output)
    # Rejected before the sandbox is even requested, so no VM boots.
    assert sandboxes.created == []


async def test_write_file_rejects_relative_paths(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "notes.md", "content": "x"}),
        says("done"),
    )
    result = await run(agent, "go")
    output = next(i for i in result.new_items if i.type == "tool_call_output_item")
    assert "invalid path" in str(output.output)


async def test_list_files_marks_directories(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file(), sandbox_list_files()],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_write_file", {"path": "/workspace/sub/a.txt", "content": "x"}, call_id="c1"
        ),
        calls_tool("sandbox_list_files", {"path": "/workspace"}, call_id="c2"),
        says("done"),
    )
    result = await run(agent, "go")
    listing = [str(i.output) for i in result.new_items if i.type == "tool_call_output_item"][-1]
    assert listing == "d  /workspace/sub"


async def test_read_file_refuses_huge_files(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_read_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_read_file", {"path": "/workspace/big.bin"}),
        says("done"),
    )
    ws = WorkspaceContext()
    # Seed a >1 MiB file straight into the bind mount once the VM exists.
    from openai_agents_msb import InputFile

    ws.input_files = [InputFile("big.bin", b"x" * (1024 * 1024 + 1))]
    result = await run(agent, "go", ws)
    output = next(i for i in result.new_items if i.type == "tool_call_output_item")
    assert "File too large" in str(output.output)
