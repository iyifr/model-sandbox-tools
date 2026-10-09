"""Finding the sandbox config an agent graph asks for."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import Agent

from .core import SandboxConfig

__all__ = [
    "DEFAULT_SANDBOX_CONFIG",
    "MST_SANDBOX_CONFIG_ATTR",
    "MST_TOOL_ATTR",
    "DiscoveredSandbox",
    "config_key",
    "discover_sandbox_config",
]

# Markers that `sandbox_config()` and the other tools set on their FunctionTool,
# so run() can recognise MST tools on an arbitrary agent.
MST_TOOL_ATTR = "__mst_tool__"
MST_SANDBOX_CONFIG_ATTR = "__mst_sandbox_config__"

DEFAULT_SANDBOX_CONFIG = SandboxConfig(image="python:3.12-slim", interpreter="python3")
"""Used when an agent has sandbox tools but no `sandbox_config()` to configure them."""


def config_key(c: SandboxConfig) -> tuple[Any, ...]:
    """The parts of a config that force a separate VM."""
    return (
        c.image,
        c.interpreter,
        c.cpus or 1,
        c.memory or 256,
        repr(c.network),
        tuple(sorted(c.packages)),
    )


@dataclass(frozen=True)
class DiscoveredSandbox:
    config: SandboxConfig | None
    """Config from `sandbox_config()` tools; None if none were found."""
    has_tools: bool
    """Whether any MST tool is reachable."""


def discover_sandbox_config(agent: Agent[Any]) -> DiscoveredSandbox:
    """Collects sandbox tools from the agent and every agent reachable through handoffs.

    Agents wrapped with `as_tool()` can't be inspected; their tools get the
    sandbox lazily, from whichever config the run settles on.
    """
    configs: list[SandboxConfig] = []
    has_tools = False
    seen: set[int] = set()

    def visit(a: Agent[Any]) -> None:
        nonlocal has_tools
        if id(a) in seen:
            return
        seen.add(id(a))
        for tool in a.tools:
            if getattr(tool, MST_TOOL_ATTR, False):
                has_tools = True
            config = getattr(tool, MST_SANDBOX_CONFIG_ATTR, None)
            if config is not None:
                configs.append(config)
        for handoff in a.handoffs or []:
            target = getattr(handoff, "agent", handoff)
            if isinstance(target, Agent):
                visit(target)

    visit(agent)

    if len(configs) > 1:
        first = config_key(configs[0])
        if any(config_key(c) != first for c in configs):
            raise ValueError(
                "[mst] All sandbox_config() tools reachable from an agent (including "
                "handoffs) must use an identical SandboxConfig"
            )
    return DiscoveredSandbox(config=configs[0] if configs else None, has_tools=has_tools)
