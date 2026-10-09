"""Shared wiring for the MST tools."""

from __future__ import annotations

from typing import Any, Callable

from agents import FunctionTool, RunContextWrapper, function_tool

from .._discover import MST_SANDBOX_CONFIG_ATTR, MST_TOOL_ATTR
from ..core import SandboxConfig, SandboxSetupError

__all__ = ["mark_mst_tool", "mst_tool", "setup_errors_fail_the_run"]


def setup_errors_fail_the_run(_context: RunContextWrapper[Any], error: Exception) -> str:
    """Sandbox setup failures fail the run; ordinary tool errors go back to the model.

    Re-raising here means the Agents SDK wraps the error in a `UserError`;
    `run()` unwraps it so callers see the original `SandboxSetupError`.
    """
    if isinstance(error, SandboxSetupError):
        raise error
    # Same text as the SDK's default error function.
    return f"An error occurred while running the tool. Please try again. Error: {error!s}"


def mark_mst_tool(tool: FunctionTool, config: SandboxConfig | None = None) -> FunctionTool:
    setattr(tool, MST_TOOL_ATTR, True)
    if config is not None:
        setattr(tool, MST_SANDBOX_CONFIG_ATTR, config)
    return tool


def mst_tool(
    func: Callable[..., Any],
    *,
    name: str,
    description: str,
    config: SandboxConfig | None = None,
) -> FunctionTool:
    """Like `function_tool()`, but marked so `run()` can find it and with setup
    errors routed past the model."""
    tool = function_tool(
        func,
        name_override=name,
        description_override=description,
        failure_error_function=setup_errors_fail_the_run,
    )
    assert isinstance(tool, FunctionTool)
    return mark_mst_tool(tool, config)
