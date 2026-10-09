"""The `sandbox_read_file` tool."""

from __future__ import annotations

from agents import FunctionTool

from ..core import get_active_sandbox
from ._base import mst_tool

__all__ = ["sandbox_read_file"]

MAX_READ_BYTES = 1024 * 1024


def sandbox_read_file() -> FunctionTool:
    """A tool that reads a text file from the sandbox."""

    async def read_file(path: str) -> str:
        """Read a text file from the sandbox.

        Args:
            path: Absolute path to the file in the sandbox
        """
        fs = (await get_active_sandbox()).fs
        meta = await fs.stat(path)
        if meta.size > MAX_READ_BYTES:
            return (
                f"[mst] File too large ({meta.size} bytes). "
                "Use sandbox_run with Python for files over 1 MiB."
            )
        return await fs.read_text(path)

    return mst_tool(
        read_file,
        name="sandbox_read_file",
        description=(
            "Read a text file from the sandbox. For binary files (docx, pdf) use "
            "sandbox_run with Python instead."
        ),
    )
