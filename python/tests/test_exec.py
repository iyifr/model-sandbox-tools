"""Output capping, the YAML result format, and exec timeouts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openai_agents_msb import SandboxConfig
from openai_agents_msb._exec import CappedOutput, exec_to_yaml
from openai_agents_msb.core import format_yaml

from .conftest import FakeSandbox


def test_short_output_is_untouched() -> None:
    out = CappedOutput(1024)
    out.push(b"hello world")
    assert str(out) == "hello world"


def test_output_under_the_cap_is_not_elided() -> None:
    out = CappedOutput(100)
    out.push(b"x" * 100)
    assert str(out) == "x" * 100
    assert "omitted" not in str(out)


def test_middle_is_dropped_and_counted() -> None:
    out = CappedOutput(100)
    out.push(b"A" * 50 + b"M" * 400 + b"Z" * 50)
    text = str(out)
    assert text.startswith("A" * 50)
    assert text.endswith("Z" * 50)
    assert "400 bytes omitted" in text


def test_cap_holds_across_many_small_chunks() -> None:
    out = CappedOutput(100)
    for _ in range(1000):
        out.push(b"0123456789")
    text = str(out)
    # 50 head + 50 tail + the notice; nowhere near the 10 KB pushed in.
    assert len(text) < 400
    assert "9900 bytes omitted" in text


def test_head_and_tail_survive_chunk_boundaries() -> None:
    out = CappedOutput(20)
    out.push(b"start")
    out.push(b"-" * 100)
    out.push(b"end")
    text = str(out)
    assert text.startswith("start")
    assert text.endswith("end")


def test_invalid_utf8_does_not_raise() -> None:
    out = CappedOutput(64)
    out.push(b"\xff\xfe binary")
    assert "binary" in str(out)


def test_yaml_quotes_empty_streams() -> None:
    assert format_yaml(0, "", "") == 'exit_code: 0\nstdout: ""\nstderr: ""'


def test_yaml_indents_block_scalars() -> None:
    assert format_yaml(1, "a\nb", "") == 'exit_code: 1\nstdout: |\n  a\n  b\nstderr: ""'


def fake(tmp_path: Path, **kwargs: object) -> FakeSandbox:
    return FakeSandbox(
        name="mst-test",
        config=SandboxConfig(image="i", interpreter="python3"),
        host_dir=tmp_path,
        guest_root=tmp_path,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_exec_reports_stdout_and_exit_code(tmp_path: Path) -> None:
    sb = fake(tmp_path, script_output=b"hi\n", exit_code=3)
    result = await exec_to_yaml(
        sb, "python3", ["/tmp/s"], timeout_secs=5, max_output_bytes=1024  # type: ignore[arg-type]
    )
    assert "exit_code: 3" in result
    assert "hi" in result


async def test_timeout_reports_124_and_kills_the_command(tmp_path: Path) -> None:
    sb = fake(tmp_path)
    sb.exec_hangs = True
    result = await exec_to_yaml(
        sb, "python3", ["/tmp/s"], timeout_secs=0.05, max_output_bytes=1024  # type: ignore[arg-type]
    )
    assert "exit_code: 124" in result
    assert "timed out after 0.05s" in result
    assert sb.kills == 1


async def test_cancelling_the_run_kills_the_command(tmp_path: Path) -> None:
    sb = fake(tmp_path)
    sb.exec_hangs = True
    task = asyncio.ensure_future(
        exec_to_yaml(
            sb, "python3", ["/tmp/s"], timeout_secs=30, max_output_bytes=1024  # type: ignore[arg-type]
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sb.kills == 1
