"""The `sandbox_exec` tool: run a shell command in the run's VM."""

from __future__ import annotations

from agents import FunctionTool

from .._exec import DEFAULT_MAX_OUTPUT_BYTES, exec_to_yaml
from ..core import get_active_sandbox
from ._base import mst_tool

__all__ = ["sandbox_exec"]

DEFAULT_TIMEOUT_SECS = 600.0


def sandbox_exec(
    *,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> FunctionTool:
    """A tool that runs a shell command in the sandbox.

    Args:
        timeout_secs: Kill the command after this many seconds.
        max_output_bytes: Max bytes of stdout and of stderr returned to the model;
            the middle is dropped.
    """

    async def run_command(command: str) -> str:
        """Run a shell command in the sandbox.

        Args:
            command: Shell command to run (e.g. "python3 -m pytest /workspace/tests/ -v")
        """
        sb = await get_active_sandbox()
        return await exec_to_yaml(
            sb,
            "/bin/sh",
            ["-c", command],
            timeout_secs=timeout_secs,
            max_output_bytes=max_output_bytes,
        )

    return mst_tool(
        run_command,
        name="sandbox_exec",
        description=(
            "Run a shell command in the sandbox and return exit code, stdout, and stderr "
            "as YAML. Use for invoking installed binaries and CLI tools (e.g. pytest, "
            "black, node, ffmpeg). For writing and running a custom script use "
            "sandbox_run instead."
        ),
    )
