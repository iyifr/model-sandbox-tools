"""The run lifecycle: lazy VMs, teardown, config errors, isolation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agents import Agent, FunctionTool, RunConfig, UserError

from openai_agents_msb import (
    Allow,
    FileOutput,
    InputFile,
    SandboxSetupError,
    WorkspaceContext,
    run,
    sandbox_config,
    sandbox_exec,
    sandbox_list_files,
    sandbox_read_file,
    sandbox_write_file,
)

from .conftest import SandboxRecorder, agent_calling, calls_tool, says, scripted

PY = {"image": "python:3.12-slim", "interpreter": "python3"}


def script_agent(**config: object) -> Agent[object]:
    return agent_calling(
        [sandbox_config(**{**PY, **config})],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print('hi')"}),
        says("done"),
    )


# -- lazy VMs --------------------------------------------------------------------------


async def test_chat_only_turns_boot_nothing(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling([sandbox_config(**PY)], says("just chatting"))  # type: ignore[arg-type]
    result = await run(agent, "hello")
    assert result.final_output == "just chatting"
    assert sandboxes.created == []


async def test_the_first_tool_call_boots_one_vm(sandboxes: SandboxRecorder) -> None:
    await run(script_agent(), "go")
    assert len(sandboxes.created) == 1


async def test_many_tool_calls_share_one_vm(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print(1)"}, call_id="c1"),
        calls_tool("sandbox_exec", {"command": "ls"}, call_id="c2"),
        says("done"),
    )
    await run(agent, "go")
    assert len(sandboxes.created) == 1
    assert len(sandboxes.only.execs) == 2


async def test_exec_only_agents_get_the_default_image(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_exec()],
        calls_tool("sandbox_exec", {"command": "ls"}),
        says("done"),
    )
    await run(agent, "go")
    assert sandboxes.only.config.image == "python:3.12-slim"


# -- teardown --------------------------------------------------------------------------


async def test_the_vm_is_stopped_and_removed(sandboxes: SandboxRecorder) -> None:
    await run(script_agent(), "go")
    assert sandboxes.only.stopped == 1
    assert sandboxes.removed == [sandboxes.only.name]


async def test_the_temp_workspace_is_deleted(sandboxes: SandboxRecorder) -> None:
    await run(script_agent(), "go")
    assert not sandboxes.only.host_dir.exists()


async def test_input_files_reach_the_guest_workspace(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_read_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_read_file", {"path": "/workspace/in.txt"}),
        says("read it"),
    )
    ws = WorkspaceContext(input_files=[InputFile("in.txt", b"seeded")])
    result = await run(agent, "go", ws)

    tool_output = next(
        item for item in result.new_items if item.type == "tool_call_output_item"
    )
    assert tool_output.output == "seeded"


async def test_files_written_in_the_guest_come_back_out(sandboxes: SandboxRecorder) -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/out.md", "content": "# hi"}),
        says("wrote it"),
    )
    await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    assert [(o.file_name, o.data) for o in outputs] == [("out.md", b"# hi")]


async def test_unchanged_inputs_are_not_reported_as_outputs(sandboxes: SandboxRecorder) -> None:
    outputs: list[FileOutput] = []
    ws = WorkspaceContext(
        input_files=[InputFile("in.txt", b"untouched")], on_file_output=outputs.append
    )
    await run(script_agent(), "go", ws)
    assert outputs == []


async def test_the_vm_stops_before_outputs_are_read(sandboxes: SandboxRecorder) -> None:
    """Nothing in the guest may change /workspace while outputs are being read."""
    stopped_when_read: list[int] = []
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/out.md", "content": "x"}),
        says("done"),
    )
    ws = WorkspaceContext(
        on_file_output=lambda _: stopped_when_read.append(sandboxes.only.stopped)
    )
    await run(agent, "go", ws)
    assert stopped_when_read == [1]


# -- config errors ---------------------------------------------------------------------


async def test_mismatched_configs_raise_before_the_model_runs(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [
            sandbox_config(image="python:3.12-slim", interpreter="python3"),
            sandbox_config(image="ubuntu", interpreter="bash"),
        ],
        says("never reached"),
    )
    with pytest.raises(ValueError, match="identical SandboxConfig"):
        await run(agent, "go")
    assert sandboxes.created == []


async def test_mismatched_configs_across_handoffs_raise(sandboxes: SandboxRecorder) -> None:
    specialist = agent_calling(
        [sandbox_config(image="ubuntu", interpreter="bash")], says("x"), name="specialist"
    )
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        says("x"),
        handoffs=[specialist],
    )
    with pytest.raises(ValueError, match="identical SandboxConfig"):
        await run(agent, "go")


async def test_identical_configs_across_handoffs_are_fine(sandboxes: SandboxRecorder) -> None:
    specialist = agent_calling([sandbox_config(**PY)], says("x"), name="specialist")  # type: ignore[arg-type]
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        says("ok"),
        handoffs=[specialist],
    )
    assert (await run(agent, "go")).final_output == "ok"


async def test_invalid_network_raises_before_the_model_runs(sandboxes: SandboxRecorder) -> None:
    agent = script_agent(network=Allow(["not a host"]))
    with pytest.raises(ValueError, match="network allowlist"):
        await run(agent, "go")
    assert sandboxes.created == []


async def test_network_none_with_packages_raises(sandboxes: SandboxRecorder) -> None:
    agent = script_agent(network="none", packages=["requests"])
    with pytest.raises(ValueError, match="incompatible with `packages`"):
        await run(agent, "go")


async def test_unsafe_input_names_raise_before_the_model_runs(
    sandboxes: SandboxRecorder,
) -> None:
    ws = WorkspaceContext(input_files=[InputFile("../escape", b"x")])
    with pytest.raises(ValueError, match="Invalid filename"):
        await run(script_agent(), "go", ws)
    assert sandboxes.created == []


# -- the reserved run_config.sandbox ---------------------------------------------------


async def test_run_config_sandbox_is_reserved(sandboxes: SandboxRecorder) -> None:
    with pytest.raises(UserError, match="reserved by openai-agents"):
        await run(script_agent(), "go", None, run_config=RunConfig(sandbox={}))  # type: ignore[arg-type]


async def test_run_config_sandbox_is_reserved_as_a_dict(sandboxes: SandboxRecorder) -> None:
    with pytest.raises(UserError, match="reserved by openai-agents"):
        await run(script_agent(), "go", None, run_config={"sandbox": {}})


async def test_other_run_config_passes_through(sandboxes: SandboxRecorder) -> None:
    result = await run(script_agent(), "go", None, run_config=RunConfig(workflow_name="mine"))
    assert result.final_output == "done"


# -- setup failures --------------------------------------------------------------------


async def test_setup_failures_fail_the_run_unwrapped(
    sandboxes: SandboxRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK wraps tool exceptions in UserError; run() hands back the original."""
    import openai_agents_msb._run as run_module

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("no hypervisor")

    monkeypatch.setattr(run_module, "_create_sandbox", boom)
    with pytest.raises(SandboxSetupError, match="no hypervisor"):
        await run(script_agent(), "go")


async def test_failed_pip_install_fails_the_run(
    sandboxes: SandboxRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .conftest import FakeSandbox, ShellResult

    async def failing_install(self: FakeSandbox, script: str, **_: object) -> ShellResult:
        self.shells.append(script)
        return ShellResult(success=False, exit_code=1, stderr_text="no such package")

    monkeypatch.setattr(FakeSandbox, "shell", failing_install)
    with pytest.raises(SandboxSetupError, match="pip install failed"):
        await run(script_agent(network="public", packages=["nope"]), "go")


async def test_ordinary_tool_errors_go_back_to_the_model(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_read_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_read_file", {"path": "/workspace/missing.txt"}),
        says("recovered"),
    )
    assert (await run(agent, "go")).final_output == "recovered"


async def test_the_vm_is_cleaned_up_when_the_run_fails(sandboxes: SandboxRecorder) -> None:
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print(1)"}),
        # No step for the second model turn, so the run fails after the VM booted.
    )
    with pytest.raises(Exception):
        await run(agent, "go")
    assert sandboxes.only.stopped == 1
    assert not sandboxes.only.host_dir.exists()


async def test_a_failed_run_emits_no_outputs(sandboxes: SandboxRecorder) -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/out.md", "content": "x"}),
    )
    with pytest.raises(Exception):
        await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    assert outputs == []


# -- tools without a run ---------------------------------------------------------------


async def test_tools_outside_a_run_say_so() -> None:
    tool = sandbox_list_files()
    with pytest.raises(SandboxSetupError, match="No active sandbox"):
        await _invoke(tool, '{"path": "/workspace"}')


async def _invoke(tool: FunctionTool, args: str) -> Any:
    from agents.tool_context import ToolContext

    ctx: ToolContext[None] = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="1", tool_arguments=args
    )
    return await tool.on_invoke_tool(ctx, args)


# -- isolation -------------------------------------------------------------------------


async def test_concurrent_runs_get_their_own_vm(sandboxes: SandboxRecorder) -> None:
    a = await asyncio.gather(
        run(script_agent(), "a"),
        run(script_agent(), "b"),
        run(script_agent(), "c"),
    )
    assert [r.final_output for r in a] == ["done", "done", "done"]
    assert len({sb.name for sb in sandboxes.created}) == 3


async def test_concurrent_runs_keep_their_own_workspace(sandboxes: SandboxRecorder) -> None:
    def writer(content: str) -> Agent[object]:
        return agent_calling(
            [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
            calls_tool("sandbox_write_file", {"path": "/workspace/out.txt", "content": content}),
            says("done"),
        )

    first: list[FileOutput] = []
    second: list[FileOutput] = []
    await asyncio.gather(
        run(writer("one"), "a", WorkspaceContext(on_file_output=first.append)),
        run(writer("two"), "b", WorkspaceContext(on_file_output=second.append)),
    )
    assert [o.data for o in first] == [b"one"]
    assert [o.data for o in second] == [b"two"]


# -- tool concurrency ------------------------------------------------------------------


async def test_sandbox_tools_run_one_at_a_time_by_default(sandboxes: SandboxRecorder) -> None:
    from openai_agents_msb._run import _normalize_run_config

    config = _normalize_run_config(None, has_tools=True)
    assert config is not None
    assert config.tool_execution is not None
    assert config.tool_execution.max_function_tool_concurrency == 1


async def test_explicit_tool_concurrency_is_respected() -> None:
    from agents import ToolExecutionConfig

    from openai_agents_msb._run import _normalize_run_config

    given = RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=4))
    config = _normalize_run_config(given, has_tools=True)
    assert config is not None and config.tool_execution is not None
    assert config.tool_execution.max_function_tool_concurrency == 4


async def test_agents_without_sandbox_tools_are_left_alone() -> None:
    from openai_agents_msb._run import _normalize_run_config

    assert _normalize_run_config(None, has_tools=False) is None
