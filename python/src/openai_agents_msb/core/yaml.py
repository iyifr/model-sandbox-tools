"""The YAML shape that exec results take when they go back to the model."""

from __future__ import annotations

__all__ = ["format_yaml"]


def _block(s: str) -> str:
    if not s:
        return '""'
    indented = "\n".join(f"  {line}" for line in s.split("\n"))
    return f"|\n{indented}"


def format_yaml(exit_code: int, stdout: str, stderr: str) -> str:
    return "\n".join(
        [
            f"exit_code: {exit_code}",
            f"stdout: {_block(stdout)}",
            f"stderr: {_block(stderr)}",
        ]
    )
