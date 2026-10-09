"""Configuration and payload types for the sandbox tools and `run()`."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union

from microsandbox import Network

__all__ = [
    "Allow",
    "FileOutput",
    "InputFile",
    "NetworkConfig",
    "SandboxConfig",
    "SandboxSecret",
    "SandboxVolumeMount",
    "WorkspaceContext",
    "WorkspaceInput",
]


@dataclass(frozen=True)
class Allow:
    """Allowlist shorthand for `SandboxConfig.network`.

    Entries are hostnames (`api.example.com`), wildcards (`*.example.com`),
    bare IPs or CIDR blocks (`10.0.0.0/8`). Hosts needed by `secrets` and
    `packages` are merged in automatically.
    """

    hosts: Sequence[str]


NetworkConfig = Union[
    str,  # 'none' | 'public'
    bool,  # True = 'public', False = 'none'
    Allow,
    Network,  # a microsandbox policy, passed through unchanged
]
"""How the sandbox may reach the network. Defaults to no network at all."""


@dataclass(frozen=True)
class SandboxSecret:
    """A secret the guest can use without ever seeing its value.

    Code in the VM reads a placeholder from `env`; microsandbox substitutes the
    real value into requests to `host` only. `host` is added to the network
    allowlist automatically.
    """

    env: str
    value: str
    host: str


@dataclass(frozen=True)
class SandboxVolumeMount:
    """A named microsandbox volume mounted into the guest."""

    name: str
    mount_path: str
    create_if_missing: bool = True
    readonly: bool = False


@dataclass(frozen=True)
class SandboxConfig:
    """The sandbox a run should use, declared by `sandbox_config()`.

    Every `sandbox_config()` tool reachable from an agent must declare the same
    config; mismatches raise before the model runs.
    """

    image: str
    interpreter: str
    cpus: int | None = None
    memory: int | None = None
    network: NetworkConfig | None = None
    timeout_secs: float | None = None
    max_output_bytes: int | None = None
    """Max bytes of stdout and of stderr returned to the model; the middle is dropped."""
    env: Mapping[str, str] | None = None
    secrets: Sequence[SandboxSecret] = ()
    packages: Sequence[str] = ()
    """Installed with `pip install` when the VM starts."""
    volumes: Sequence[SandboxVolumeMount] = ()
    persist: bool = False
    create_kwargs: Mapping[str, Any] = field(default_factory=dict)
    """Extra keyword arguments merged into `microsandbox.Sandbox.create()`.

    The escape hatch for anything this config does not model. Takes precedence
    over the arguments derived from the fields above.
    """


@dataclass(frozen=True)
class InputFile:
    """A file to seed into `/workspace` before the run."""

    name: str
    data: bytes


WorkspaceInput = Union[InputFile, str, os.PathLike[str]]
"""An input file, or a path whose basename becomes the name in `/workspace`."""


@dataclass(frozen=True)
class FileOutput:
    """A file that the run created or changed in `/workspace`."""

    file_name: str
    """Path relative to /workspace, e.g. 'reports/q1/summary.md'."""
    size: int
    path: str
    """Host path of the file. Only valid until the `on_file_output` callback returns."""
    data: bytes | None
    """File contents; None for files of 2 GiB or more (read them from `path`)."""
    version: int = 1


@dataclass
class WorkspaceContext:
    """The host-side `/workspace` for a run: what goes in, and what comes out."""

    input_files: Sequence[WorkspaceInput] = ()
    on_file_output: Callable[[FileOutput], Any | Awaitable[Any]] | None = None
    """Called once per new or changed file after the run. May be sync or async."""
    on_workspace_snapshot: Callable[[list[str]], Any | Awaitable[Any]] | None = None
    """Called with every sandbox workspace path after the run (for UI file trees)."""
    sandbox_name: str | None = None
    """Stable sandbox name; required to persist a VM across `run()` calls."""
    skip_input_seed: bool = False
    """Skip writing `input_files`; use on follow-up turns of a persistent session."""
    workspace_root: str | os.PathLike[str] | None = None
    """Directory holding persistent session workspaces, one folder per `sandbox_name`,
    and temp workspaces for other runs. Defaults to `~/.mst/workspaces`. Use the same
    value for every turn of a session and for `list_sessions()` / `end_session()`.
    """


def input_name(f: WorkspaceInput) -> str:
    """The name an input will take in `/workspace`, without reading the file."""
    if isinstance(f, InputFile):
        return f.name
    return Path(os.fspath(f)).name


def normalize_input(f: WorkspaceInput) -> InputFile:
    """Read a path-like input into memory; pass `InputFile` through unchanged."""
    if isinstance(f, InputFile):
        return f
    path = Path(os.fspath(f))
    return InputFile(name=path.name, data=path.read_bytes())
