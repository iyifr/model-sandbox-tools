"""The `sandbox_run` tool, and the config that describes the VM it runs in."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from agents import FunctionTool

from .._exec import DEFAULT_MAX_OUTPUT_BYTES, exec_to_yaml
from ..core import (
    NetworkConfig,
    SandboxConfig,
    SandboxSecret,
    SandboxVolumeMount,
    get_active_sandbox,
)
from ._base import mst_tool

__all__ = ["sandbox_config"]

DEFAULT_TIMEOUT_SECS = 30.0


def sandbox_config(
    *,
    image: str,
    interpreter: str,
    cpus: int | None = None,
    memory: int | None = None,
    network: NetworkConfig | None = None,
    timeout_secs: float | None = None,
    max_output_bytes: int | None = None,
    env: Mapping[str, str] | None = None,
    secrets: Sequence[SandboxSecret] = (),
    packages: Sequence[str] = (),
    volumes: Sequence[SandboxVolumeMount] = (),
    persist: bool = False,
    create_kwargs: Mapping[str, Any] | None = None,
) -> FunctionTool:
    """A tool that runs a script in the sandbox, and configures the run's VM.

    Every `sandbox_config()` reachable from an agent must pass the same options.
    Agents with only `sandbox_exec` / read / write tools share the run's VM; if
    no `sandbox_config()` is reachable at all, the run uses `python:3.12-slim`.

    Args:
        image: Container image for the VM, e.g. `python:3.12-slim`.
        interpreter: Program that runs the script, e.g. `python3`.
        cpus: vCPUs for the VM. Defaults to 1.
        memory: Memory for the VM in MiB. Defaults to 256.
        network: `'none'` (the default), `'public'`, `Allow([...])`, a bool, or a
            microsandbox `Network`.
        timeout_secs: Kill a script after this many seconds. Defaults to 30.
        max_output_bytes: Max bytes of stdout and of stderr returned to the model.
        env: Environment variables for the VM.
        secrets: Values the guest sees only as placeholders.
        packages: Installed with `pip install` when the VM starts.
        volumes: Named microsandbox volumes to mount.
        persist: Keep the VM between runs; needs `WorkspaceContext(sandbox_name=...)`.
        create_kwargs: Extra arguments merged into `microsandbox.Sandbox.create()`.
    """
    config = SandboxConfig(
        image=image,
        interpreter=interpreter,
        cpus=cpus,
        memory=memory,
        network=network,
        timeout_secs=timeout_secs,
        max_output_bytes=max_output_bytes,
        env=env,
        secrets=tuple(secrets),
        packages=tuple(packages),
        volumes=tuple(volumes),
        persist=persist,
        create_kwargs=dict(create_kwargs or {}),
    )
    timeout = config.timeout_secs if config.timeout_secs is not None else DEFAULT_TIMEOUT_SECS
    max_bytes = (
        config.max_output_bytes
        if config.max_output_bytes is not None
        else DEFAULT_MAX_OUTPUT_BYTES
    )

    async def run_script(script: str) -> str:
        """Run a script in the sandbox.

        Args:
            script: The script content to execute
        """
        sb = await get_active_sandbox(config)
        script_path = f"/tmp/mst_script_{uuid.uuid4().hex}"
        await sb.fs.write(script_path, script.encode("utf-8"))
        try:
            return await exec_to_yaml(
                sb,
                config.interpreter,
                [script_path],
                timeout_secs=timeout,
                max_output_bytes=max_bytes,
            )
        finally:
            try:
                await sb.fs.remove(script_path)
            except Exception:
                pass

    return mst_tool(
        run_script,
        name="sandbox_run",
        description=(
            "Execute a script inside an isolated sandbox. "
            "Returns exit code, stdout, and stderr as YAML."
        ),
        config=config,
    )
