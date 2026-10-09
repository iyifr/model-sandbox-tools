"""The `sandbox_write_file` tool."""

from __future__ import annotations

from agents import FunctionTool

from ..core import get_active_sandbox
from ._base import mst_tool

__all__ = ["sandbox_write_file"]


def sandbox_write_file() -> FunctionTool:
    """A tool that writes a text file into the sandbox."""

    async def write_file(path: str, content: str) -> str:
        """Write text content to a file in the sandbox.

        Args:
            path: Absolute path to write to (e.g. /workspace/report.md)
            content: Text content to write
        """
        if not path.startswith("/") or ".." in path:
            return f"[mst] invalid path: {path!r}"

        sb = await get_active_sandbox()

        # Ensure every ancestor directory exists before writing.
        segments = [s for s in path.split("/") if s][:-1]
        current = ""
        for segment in segments:
            current += f"/{segment}"
            try:
                await sb.fs.mkdir(current)
            except Exception:
                pass

        data = content.encode("utf-8")
        await sb.fs.write(path, data)
        return f"written {path} ({len(data)} bytes)"

    return mst_tool(
        write_file,
        name="sandbox_write_file",
        description=(
            "Write text content directly to a file in the sandbox. Use for writing "
            "reports, configs, JSON, markdown, or any finished text artifact. For "
            "binary file manipulation use sandbox_run with Python instead."
        ),
    )
