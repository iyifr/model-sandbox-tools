"""Running commands in the guest and capping what comes back."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from microsandbox import ExecEventType

from .core import format_yaml

if TYPE_CHECKING:
    from microsandbox import Sandbox

__all__ = ["DEFAULT_MAX_OUTPUT_BYTES", "CappedOutput", "exec_to_yaml"]

DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024
TIMEOUT_EXIT_CODE = 124


class CappedOutput:
    """Keeps the first and last `max_bytes / 2` of a stream.

    Memory stays bounded however much the command prints; the dropped middle is
    reported as a byte count so the model knows output was elided.
    """

    def __init__(self, max_bytes: int) -> None:
        self._half = max_bytes // 2
        self._head: list[bytes] = []
        self._head_bytes = 0
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._total = 0

    def push(self, data: bytes) -> None:
        self._total += len(data)
        chunk = data
        if self._head_bytes < self._half:
            take = min(self._half - self._head_bytes, len(chunk))
            self._head.append(chunk[:take])
            self._head_bytes += take
            chunk = chunk[take:]
        if not chunk:
            return
        self._tail.append(chunk)
        self._tail_bytes += len(chunk)
        while len(self._tail) > 1 and self._tail_bytes - len(self._tail[0]) >= self._half:
            self._tail_bytes -= len(self._tail.popleft())

    def __str__(self) -> str:
        tail = b"".join(self._tail)
        if len(tail) > self._half:
            tail = tail[len(tail) - self._half :]
        head = b"".join(self._head)
        omitted = self._total - self._head_bytes - len(tail)
        decoded_head = head.decode("utf-8", errors="replace")
        decoded_tail = tail.decode("utf-8", errors="replace")
        if omitted <= 0:
            return decoded_head + decoded_tail
        return (
            f"{decoded_head}\n... [mst] {omitted} bytes omitted. "
            "Redirect large output to a file and inspect it with grep/head/tail ...\n"
            f"{decoded_tail}"
        )


async def exec_to_yaml(
    sb: "Sandbox",
    cmd: str,
    args: list[str],
    *,
    timeout_secs: float,
    max_output_bytes: int,
) -> str:
    """Run a command and return the YAML tool result.

    Kills the process on timeout (reported as exit 124) or when the run is
    cancelled. The guest's own exec timeout is not used, so that partial output
    survives: the timer lives here and kills the handle.
    """
    handle = await sb.exec_stream(cmd, args)

    stdout = CappedOutput(max_output_bytes)
    stderr = CappedOutput(max_output_bytes)
    code = -1
    timed_out = False

    async def kill_after_timeout() -> None:
        nonlocal timed_out
        await asyncio.sleep(timeout_secs)
        timed_out = True
        await _kill(handle)

    timer = asyncio.ensure_future(kill_after_timeout())
    try:
        async for event in handle:
            if event.event_type == ExecEventType.STDOUT and event.data:
                stdout.push(event.data)
            elif event.event_type == ExecEventType.STDERR and event.data:
                stderr.push(event.data)
            elif event.event_type == ExecEventType.EXITED and event.code is not None:
                code = event.code
    except asyncio.CancelledError:
        # Cancelling the run must not leave the command running in the VM.
        await _kill(handle)
        raise
    finally:
        timer.cancel()

    if timed_out:
        return format_yaml(
            TIMEOUT_EXIT_CODE,
            str(stdout),
            f"{stderr}exec timed out after {timeout_secs}s",
        )
    return format_yaml(code, str(stdout), str(stderr))


async def _kill(handle: object) -> None:
    try:
        await handle.kill()  # type: ignore[attr-defined]
    except Exception:
        pass
