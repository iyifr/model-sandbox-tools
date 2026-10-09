"""Name checks for anything that becomes a path on the host."""

from __future__ import annotations

__all__ = ["assert_safe_filename", "assert_session_name"]


def assert_safe_filename(name: str) -> None:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"[mst] Invalid filename: {name!r}")


def assert_session_name(name: str) -> None:
    """Session names become directory names, and dot-names are reserved for MST."""
    assert_safe_filename(name)
    if name.startswith("."):
        raise ValueError(f"[mst] Invalid session name: {name!r}")
