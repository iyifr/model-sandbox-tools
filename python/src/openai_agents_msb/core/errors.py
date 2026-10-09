"""Errors that fail the run instead of going back to the model."""

from __future__ import annotations

__all__ = ["SandboxSetupError", "SessionNotFoundError"]


class SandboxSetupError(Exception):
    """Sandbox setup or configuration failed.

    Tools re-raise this instead of reporting it to the model, so it fails the
    run for the developer. `run()` unwraps it from the `UserError` that the
    Agents SDK wraps tool exceptions in.
    """


class SessionNotFoundError(SandboxSetupError):
    """A follow-up turn targeted a session that no longer exists."""

    def __init__(self, session_name: str, reason: str) -> None:
        super().__init__(
            f"[mst] Session {session_name!r} not found: {reason}. "
            "Re-seed it with skip_input_seed=False."
        )
        self.session_name = session_name
