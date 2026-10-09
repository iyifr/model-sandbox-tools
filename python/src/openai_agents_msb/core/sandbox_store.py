"""Per-run sandbox lookup.

The sandbox tools have no reference to the `run()` that owns them, so `run()`
publishes a provider in a `ContextVar` before handing control to `Runner`.
Each asyncio task gets its own copy of the context, so concurrent runs -- and
the task that `Runner.run_streamed()` creates -- each see their own sandbox.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Optional, Protocol

from .errors import SandboxSetupError

if TYPE_CHECKING:
    from microsandbox import Sandbox

    from .types import SandboxConfig

__all__ = ["SandboxProvider", "get_active_sandbox", "reset_provider", "set_provider"]


class SandboxProvider(Protocol):
    """Hands out the run's sandbox, creating it on first use."""

    async def get(self, requested: Optional["SandboxConfig"] = None) -> "Sandbox":
        """`requested` is the calling `sandbox_config()`'s config; other tools pass nothing."""
        ...


_provider: ContextVar[SandboxProvider | None] = ContextVar("mst_sandbox_provider", default=None)


def set_provider(provider: SandboxProvider) -> Token[SandboxProvider | None]:
    return _provider.set(provider)


def reset_provider(token: Token[SandboxProvider | None]) -> None:
    _provider.reset(token)


async def get_active_sandbox(requested: Optional["SandboxConfig"] = None) -> "Sandbox":
    provider = _provider.get()
    if provider is None:
        raise SandboxSetupError(
            "[mst] No active sandbox. Call your agent through openai_agents_msb.run()"
        )
    return await provider.get(requested)
