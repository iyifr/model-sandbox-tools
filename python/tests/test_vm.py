"""End-to-end against a real microsandbox VM.

Run with `pytest -m vm`. Skipped unless the microsandbox runtime is installed on
this host, since it needs hardware virtualisation. The image is the package
default, so a host that has run the examples already has it cached.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from microsandbox import Sandbox, SandboxNotFoundError, is_runtime_installed

from openai_agents_msb import (
    Allow,
    FileOutput,
    InputFile,
    SessionNotFoundError,
    WorkspaceContext,
    end_session,
    list_sessions,
    run,
    run_streamed,
    sandbox_config,
    sandbox_exec,
    sandbox_read_file,
    sandbox_write_file,
)

from .conftest import agent_calling, calls_tool, says

pytestmark = [
    pytest.mark.vm,
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        not is_runtime_installed(), reason="microsandbox runtime is not installed"
    ),
]

PY = {"image": "python:3.12-slim", "interpreter": "python3"}
FETCH = "python3 -c \"import urllib.request as u; u.urlopen('https://{host}', timeout=20)\""


def tool_output(result: Any) -> str:
    return str(next(i for i in result.new_items if i.type == "tool_call_output_item").output)


@pytest.fixture(autouse=True)
async def no_leftover_sandboxes() -> Any:
    """Every test must leave no mst-* VM behind."""
    yield
    page = await Sandbox.list()
    leftovers = [s.name for s in page.sandboxes if s.name.startswith("mst-")]
    for name in leftovers:
        try:
            await Sandbox.remove(name)
        except SandboxNotFoundError:
            pass
    assert leftovers == [], f"leftover sandboxes: {leftovers}"


async def test_a_script_runs_and_reports_its_output() -> None:
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print('hello from the vm')"}),
        says("done"),
    )
    output = tool_output(await run(agent, "go"))
    assert "hello from the vm" in output
    assert "exit_code: 0" in output


async def test_a_nonzero_exit_is_reported() -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_exec", {"command": "exit 7"}),
        says("done"),
    )
    assert "exit_code: 7" in tool_output(await run(agent, "go"))


async def test_stderr_comes_back_separately() -> None:
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run",
            {"script": "import sys; print('out'); print('err', file=sys.stderr)"},
        ),
        says("done"),
    )
    output = tool_output(await run(agent, "go"))
    assert "out" in output.split("stderr:")[0]
    assert "err" in output.split("stderr:")[1]


async def test_a_file_written_in_the_vm_reaches_the_host() -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run", {"script": "open('/workspace/out.txt','w').write('report')"}
        ),
        says("done"),
    )
    await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    assert [(o.file_name, o.data) for o in outputs] == [("out.txt", b"report")]


async def test_an_input_file_is_readable_in_the_vm() -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_read_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_read_file", {"path": "/workspace/in.txt"}),
        says("done"),
    )
    ws = WorkspaceContext(input_files=[InputFile("in.txt", b"seeded bytes")])
    assert "seeded bytes" in tool_output(await run(agent, "go", ws))


async def test_an_unchanged_input_is_not_reported_back() -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "open('/workspace/in.txt').read()"}),
        says("done"),
    )
    ws = WorkspaceContext(
        input_files=[InputFile("in.txt", b"untouched")], on_file_output=outputs.append
    )
    await run(agent, "go", ws)
    assert outputs == []


async def test_a_script_timeout_reports_124() -> None:
    agent = agent_calling(
        [sandbox_config(**PY, timeout_secs=3)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run",
            {"script": "import time; print('starting', flush=True); time.sleep(120)"},
        ),
        says("done"),
    )
    output = tool_output(await run(agent, "go"))
    assert "exit_code: 124" in output
    # Output printed before the kill survives.
    assert "starting" in output


async def test_large_output_is_capped() -> None:
    agent = agent_calling(
        [sandbox_config(**PY, max_output_bytes=2048)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run",
            {"script": "for i in range(200000): print('line', i, 'xxxxxxxxxx')"},
        ),
        says("done"),
    )
    output = tool_output(await run(agent, "go"))
    assert "bytes omitted" in output
    assert len(output) < 20_000


async def test_nested_outputs_keep_their_relative_paths() -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run",
            {
                "script": "import os; os.makedirs('/workspace/a/b', exist_ok=True); "
                "open('/workspace/a/b/c.txt','w').write('deep')"
            },
        ),
        says("done"),
    )
    await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    assert [o.file_name for o in outputs] == ["a/b/c.txt"]


async def test_a_symlink_in_the_workspace_is_not_an_output() -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run",
            {
                "script": "import os; open('/workspace/real.txt','w').write('real'); "
                "os.symlink('/etc/passwd', '/workspace/link')"
            },
        ),
        says("done"),
    )
    await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    assert [o.file_name for o in outputs] == ["real.txt"]


async def test_the_workspace_is_writable_from_the_guest() -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
        calls_tool("sandbox_write_file", {"path": "/workspace/report.md", "content": "# hi"}),
        says("done"),
    )
    await run(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    assert [(o.file_name, o.data) for o in outputs] == [("report.md", b"# hi")]


async def test_no_network_by_default() -> None:
    agent = agent_calling(
        [sandbox_config(**PY), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_exec", {"command": FETCH.format(host="example.com")}),
        says("done"),
    )
    assert "exit_code: 0" not in tool_output(await run(agent, "go"))


@pytest.mark.network
async def test_public_network_reaches_the_internet() -> None:
    agent = agent_calling(
        [sandbox_config(**PY, network="public"), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_exec", {"command": FETCH.format(host="example.com")}),
        says("done"),
    )
    assert "exit_code: 0" in tool_output(await run(agent, "go"))


@pytest.mark.network
async def test_an_allowlist_permits_only_its_hosts() -> None:
    agent = agent_calling(
        [sandbox_config(**PY, network=Allow(["example.com"])), sandbox_exec()],  # type: ignore[arg-type]
        calls_tool("sandbox_exec", {"command": FETCH.format(host="www.wikipedia.org")}),
        says("done"),
    )
    assert "exit_code: 0" not in tool_output(await run(agent, "go"))


async def test_streaming_delivers_outputs_and_stops_the_vm() -> None:
    outputs: list[FileOutput] = []
    agent = agent_calling(
        [sandbox_config(**PY)],  # type: ignore[arg-type]
        calls_tool(
            "sandbox_run", {"script": "open('/workspace/s.txt','w').write('streamed')"}
        ),
        says("done"),
    )
    result = run_streamed(agent, "go", WorkspaceContext(on_file_output=outputs.append))
    async for _ in result.stream_events():
        pass
    assert [o.file_name for o in outputs] == ["s.txt"]
    assert result.final_output == "done"


async def test_a_session_survives_between_turns(workspace_root: Path) -> None:
    def agent(script: str) -> Any:
        return agent_calling(
            [sandbox_config(**PY, persist=True)],  # type: ignore[arg-type]
            calls_tool("sandbox_run", {"script": script}),
            says("done"),
        )

    name = "mst-test-session"
    try:
        first = WorkspaceContext(sandbox_name=name, workspace_root=workspace_root)
        await run(agent("open('/workspace/turn1.txt','w').write('one')"), "go", first)

        second = WorkspaceContext(
            sandbox_name=name, workspace_root=workspace_root, skip_input_seed=True
        )
        result = await run(agent("print(open('/workspace/turn1.txt').read())"), "again", second)
        assert "one" in tool_output(result)

        sessions = await list_sessions(workspace_root=workspace_root)
        assert [s.name for s in sessions] == [name]
    finally:
        await end_session(name, workspace_root=workspace_root)

    assert await list_sessions(workspace_root=workspace_root) == []


async def test_a_follow_up_on_a_deleted_session_raises(workspace_root: Path) -> None:
    agent = agent_calling(
        [sandbox_config(**PY, persist=True)],  # type: ignore[arg-type]
        calls_tool("sandbox_run", {"script": "print('hi')"}),
        says("done"),
    )
    name = "mst-test-gone"
    ws = WorkspaceContext(sandbox_name=name, workspace_root=workspace_root)
    await run(agent, "go", ws)
    await end_session(name, workspace_root=workspace_root)

    follow_up = WorkspaceContext(
        sandbox_name=name, workspace_root=workspace_root, skip_input_seed=True
    )
    with pytest.raises(SessionNotFoundError):
        await run(agent, "again", follow_up)


async def test_concurrent_runs_do_not_see_each_other_s_files() -> None:
    def agent(content: str) -> Any:
        return agent_calling(
            [sandbox_config(**PY), sandbox_write_file()],  # type: ignore[arg-type]
            calls_tool(
                "sandbox_write_file", {"path": "/workspace/out.txt", "content": content}
            ),
            says("done"),
        )

    first: list[FileOutput] = []
    second: list[FileOutput] = []
    await asyncio.gather(
        run(agent("one"), "a", WorkspaceContext(on_file_output=first.append)),
        run(agent("two"), "b", WorkspaceContext(on_file_output=second.append)),
    )
    assert [o.data for o in first] == [b"one"]
    assert [o.data for o in second] == [b"two"]


async def test_chat_only_turns_boot_nothing() -> None:
    agent = agent_calling([sandbox_config(**PY)], says("just chatting"))  # type: ignore[arg-type]
    result = await run(agent, "hello")
    assert result.final_output == "just chatting"
    page = await Sandbox.list()
    assert [s.name for s in page.sandboxes if s.name.startswith("mst-")] == []
