"""The `sandbox_list_files` tool."""

from __future__ import annotations

from agents import FunctionTool
from microsandbox import FsEntryKind

from ..core import get_active_sandbox
from ._base import mst_tool

__all__ = ["sandbox_list_files"]


def sandbox_list_files() -> FunctionTool:
    """A tool that lists a directory inside the sandbox."""

    async def list_files(path: str = "/workspace") -> str:
        """List files in a directory inside the sandbox.

        Args:
            path: Directory path to list
        """
        entries = await (await get_active_sandbox()).fs.list(path)
        return "\n".join(
            f"{'d' if e.kind == FsEntryKind.DIRECTORY else 'f'}  {e.path}" for e in entries
        )

    return mst_tool(
        list_files,
        name="sandbox_list_files",
        description="List files in a directory inside the sandbox.",
    )
