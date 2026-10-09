"""Drop-in replacements for `Runner.run*` that give the sandbox tools a VM."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import Any, Callable, Optional, cast

from agents import (
    Agent,
    RunConfig,
    RunResult,
    RunResultStreaming,
    Runner,
    StreamEvent,
    ToolExecutionConfig,
    UserError,
)
from microsandbox import Sandbox, SandboxNotFoundError, Volume

from ._discover import DEFAULT_SANDBOX_CONFIG, config_key, discover_sandbox_config
from ._network import resolve_network, to_secret_entries, validate_network
from ._session import acquire_session, touch_session_dir
from ._workspace import (
    GUEST_WORKSPACE,
    WorkspaceSnapshot,
    assert_input_names,
    clear_workspace_dir,
    create_workspace_dir,
    emit_file_outputs,
    seed_input_files,
    session_dir,
    snapshot_workspace,
)
from .core import (
    SandboxConfig,
    SandboxSetupError,
    SessionNotFoundError,
    WorkspaceContext,
    assert_session_name,
    reset_provider,
    set_provider,
)

__all__ = ["MstRunResultStreaming", "run", "run_streamed", "run_sync"]


class _Lifecycle:
    """One run's sandbox: created on first use, torn down once.

    The VM is created on the first sandbox tool call, so handoffs, `as_tool()`
    agents and tools without a `sandbox_config()` all share one VM per run, and
    chat-only turns boot nothing.
    """

    def __init__(
        self,
        workspace: WorkspaceContext | None,
        discovered_config: SandboxConfig | None,
    ) -> None:
        self._ws = workspace
        self._discovered = discovered_config

        named = workspace.sandbox_name if workspace is not None else None
        # Session settings come from the reachable sandbox_config(); as_tool()
        # agents can't be inspected.
        self.persistent = bool(
            discovered_config is not None and discovered_config.persist and named is not None
        )
        self.reusing = self.persistent and workspace is not None and workspace.skip_input_seed
        self.name = (
            named
            if named is not None
            else f"mst-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        )

        self._sandbox: asyncio.Task[Sandbox] | None = None
        self._sandbox_key: tuple[Any, ...] | None = None
        self._sb: Sandbox | None = None
        self._host_dir: Path | None = None
        self._input_snapshot: WorkspaceSnapshot = {}
        self._release_session: Callable[[], None] = lambda: None
        self._prepared: asyncio.Future[None] | None = None
        self._teardown: asyncio.Task[None] | None = None

    # -- pre-model setup ---------------------------------------------------------------

    async def prepare(self) -> None:
        """Take the session lock and fail early if a follow-up targets a dead session.

        Idempotent: `run_streamed()` awaits it from whichever of `stream_events()`
        or `wait_completed()` the caller reaches first.
        """
        if self._prepared is not None:
            await self._prepared
            return
        self._prepared = asyncio.get_running_loop().create_future()
        try:
            await self._prepare()
        except BaseException as err:
            self._prepared.set_exception(err)
            # Nobody else is waiting yet; don't let the future look unretrieved.
            self._prepared.exception()
            raise
        else:
            self._prepared.set_result(None)

    async def _prepare(self) -> None:
        if self._ws is not None and self._ws.sandbox_name is not None:
            self._release_session = await acquire_session(self._ws.sandbox_name)

        if not self.reusing:
            return

        assert self._ws is not None
        directory = session_dir(self.name, self._ws.workspace_root)
        try:
            self._host_dir = directory.resolve(strict=True)
        except OSError as err:
            raise SessionNotFoundError(self.name, "workspace directory is missing") from err
        try:
            await Sandbox.get(self.name)
        except SandboxNotFoundError as err:
            raise SessionNotFoundError(self.name, "sandbox was removed") from err
        self._input_snapshot = await snapshot_workspace(self._host_dir)

    # -- SandboxProvider ---------------------------------------------------------------

    async def get(self, requested: Optional[SandboxConfig] = None) -> Sandbox:
        config = requested or self._discovered or DEFAULT_SANDBOX_CONFIG

        if self._sandbox is not None:
            if requested is not None and config_key(requested) != self._sandbox_key:
                raise SandboxSetupError(
                    "[mst] All sandbox_config() tools used in one run must use an "
                    "identical SandboxConfig"
                )
            return await asyncio.shield(self._sandbox)

        self._sandbox_key = config_key(config)
        self._sandbox = asyncio.ensure_future(self._start(config))
        return await asyncio.shield(self._sandbox)

    async def _start(self, config: SandboxConfig) -> Sandbox:
        try:
            validate_network(config)
            return await self._boot(config)
        except SandboxSetupError:
            # Already shaped for the developer; SessionNotFoundError included.
            raise
        except Exception as err:
            raise SandboxSetupError(f"[mst] {err}") from err

    async def _boot(self, config: SandboxConfig) -> Sandbox:
        if self.reusing:
            try:
                self._sb = await Sandbox.start(self.name)
            except SandboxNotFoundError as err:
                raise SessionNotFoundError(self.name, "sandbox was removed") from err
            return self._sb

        self._host_dir = await create_workspace_dir(
            self.name,
            self.persistent,
            self._ws.workspace_root if self._ws is not None else None,
        )
        await clear_workspace_dir(self._host_dir)
        if self._ws is not None and self._ws.input_files and not self._ws.skip_input_seed:
            self._input_snapshot = await seed_input_files(self._host_dir, self._ws.input_files)

        self._sb = await _create_sandbox(self.name, config, self._host_dir, self.persistent)

        if config.packages:
            out = await self._sb.shell(f"pip install --quiet {' '.join(config.packages)}")
            if not out.success:
                raise SandboxSetupError(
                    f"[mst] pip install failed (exit {out.exit_code}):\n{out.stderr_text}"
                )
        return self._sb

    # -- teardown ----------------------------------------------------------------------

    def finish(self, *, emit: bool) -> Awaitable[None]:
        """Stop the VM, then read the workspace.

        Teardown happens once; later callers await the same work rather than
        returning early, so when `stream_events()` ends the outputs really are
        delivered. Returns an awaitable, not a coroutine, so the first caller
        does not have to be the one that finishes it.
        """
        if self._teardown is None:
            self._teardown = asyncio.ensure_future(self._finish(emit=emit))
        return asyncio.shield(self._teardown)

    async def _finish(self, *, emit: bool) -> None:
        try:
            if self._sandbox is not None:
                try:
                    await self._sandbox
                except BaseException:
                    pass
            if self._sb is not None:
                try:
                    await self._sb.stop()
                except BaseException:
                    pass
            # An ephemeral VM that died mid-run is not removed by stop().
            if self._sb is not None and not self.persistent:
                try:
                    await Sandbox.remove(self.name)
                except BaseException:
                    pass
            try:
                if emit and self._host_dir is not None:
                    await emit_file_outputs(self._host_dir, self._ws, self._input_snapshot)
            finally:
                await self._clean_host_dir()
        finally:
            self._release_session()

    async def _clean_host_dir(self) -> None:
        if self._host_dir is None:
            return
        if not self.persistent:
            await asyncio.to_thread(shutil.rmtree, self._host_dir, ignore_errors=True)
        elif self._sb is not None:
            await asyncio.to_thread(touch_session_dir, self._host_dir)


async def _create_sandbox(
    name: str,
    config: SandboxConfig,
    host_workspace_dir: Path,
    persistent: bool,
) -> Sandbox:
    volumes: dict[str, Any] = {GUEST_WORKSPACE: Volume.bind(str(host_workspace_dir))}
    for vol in config.volumes:
        try:
            await Volume.get(vol.name)
        except Exception:
            if not vol.create_if_missing:
                raise ValueError(f"[mst] Volume not found: {vol.name}") from None
            await Volume.create(vol.name)
        volumes[vol.mount_path] = Volume.named(vol.name, readonly=vol.readonly)

    kwargs: dict[str, Any] = {
        "image": config.image,
        "cpus": config.cpus or 1,
        "memory": config.memory or 256,
        "ephemeral": not persistent,
        "replace": True,
        "volumes": volumes,
        "network": resolve_network(config),
    }
    if config.env:
        kwargs["env"] = dict(config.env)
    secrets = to_secret_entries(config)
    if secrets:
        kwargs["secrets"] = secrets
    kwargs.update(config.create_kwargs)

    return await Sandbox.create(name, **kwargs)


# -- shared argument handling ----------------------------------------------------------


def _reserved_sandbox_error() -> UserError:
    return UserError(
        "[mst] `run_config.sandbox` is reserved by openai-agents for its built-in "
        "sandbox (shell/apply_patch). MST manages microsandbox via sandbox_config() "
        "tools. Remove run_config.sandbox, or use agents.Runner directly if you need "
        "the SDK sandbox instead of MST."
    )


def _normalize_run_config(
    run_config: RunConfig | dict[str, Any] | None, has_tools: bool
) -> RunConfig | None:
    if isinstance(run_config, dict):
        if run_config.get("sandbox") is not None:
            raise _reserved_sandbox_error()
        config: RunConfig | None = RunConfig(**run_config)
    elif run_config is None:
        config = None
    else:
        if run_config.sandbox is not None:
            raise _reserved_sandbox_error()
        config = run_config

    if not has_tools:
        return config

    # The tools share one VM, so run them one at a time unless the caller says otherwise.
    config = config if config is not None else RunConfig()
    execution = config.tool_execution or ToolExecutionConfig()
    if execution.max_function_tool_concurrency is None:
        execution = dataclasses.replace(execution, max_function_tool_concurrency=1)
    return dataclasses.replace(config, tool_execution=execution)


def _setup(
    agent: Agent[Any],
    workspace: WorkspaceContext | None,
    kwargs: dict[str, Any],
) -> _Lifecycle:
    """Everything that must fail before the model runs."""
    discovered = discover_sandbox_config(agent)
    if discovered.config is not None:
        validate_network(discovered.config)

    lifecycle = _Lifecycle(workspace, discovered.config)
    # A sandbox name becomes a VM name, and for a session a directory name too.
    if workspace is not None and workspace.sandbox_name is not None:
        assert_session_name(workspace.sandbox_name)
    # Checked now even though the workspace is only seeded once a sandbox tool runs.
    if workspace is not None and workspace.input_files and not workspace.skip_input_seed:
        assert_input_names(workspace.input_files)

    kwargs["run_config"] = _normalize_run_config(kwargs.get("run_config"), discovered.has_tools)
    return lifecycle


def _unwrap(err: BaseException) -> BaseException | None:
    """The original `SandboxSetupError` the SDK wrapped in a `UserError`, if any."""
    if isinstance(err, UserError) and isinstance(err.__cause__, SandboxSetupError):
        return err.__cause__
    return None


# -- public API ------------------------------------------------------------------------


async def run(
    agent: Agent[Any],
    input: Any,
    workspace: WorkspaceContext | None = None,
    **runner_kwargs: Any,
) -> RunResult:
    """Run an agent with a microsandbox VM behind its sandbox tools.

    A drop-in for `agents.Runner.run()`; every keyword argument is passed
    through unchanged. `workspace` seeds `/workspace` and collects the files
    the run produced.
    """
    lifecycle = _setup(agent, workspace, runner_kwargs)
    await lifecycle.prepare()

    token = set_provider(lifecycle)
    try:
        result = await Runner.run(agent, input, **runner_kwargs)
    except BaseException as err:
        # Clean up without masking the original error.
        await lifecycle.finish(emit=False)
        setup_error = _unwrap(err)
        if setup_error is None:
            raise
        raise setup_error from None
    finally:
        reset_provider(token)

    await lifecycle.finish(emit=True)
    return result


def run_sync(
    agent: Agent[Any],
    input: Any,
    workspace: WorkspaceContext | None = None,
    **runner_kwargs: Any,
) -> RunResult:
    """`run()` for callers with no event loop of their own. Mirrors `Runner.run_sync()`."""
    return asyncio.run(run(agent, input, workspace, **runner_kwargs))


class MstRunResultStreaming(RunResultStreaming):
    """A `RunResultStreaming` that tears the sandbox down when the stream ends.

    Teardown -- emitting file outputs and stopping the VM -- runs when the stream
    finishes, and again as a backstop when the underlying run loop finishes. The
    backstop matters because an abandoned async generator is not finalized
    promptly: without it, breaking out of `stream_events()` or cancelling the
    consumer would leave the VM running.

    Callers that don't want the events can `await wait_completed()`, and
    `async with` closes everything down deterministically.
    """

    _mst_lifecycle: _Lifecycle
    _mst_teardown: asyncio.Task[None] | None = None

    def _arm_teardown(self) -> None:
        """Tear down once the run loop ends, whatever the consumer does."""
        task = self.run_loop_task

        async def watch() -> None:
            # Always wait for the run loop: an abandoned stream is finalized as
            # soon as it loses its last reference, which can be long before the
            # agent is done, and the VM must outlive the run.
            if task is not None:
                try:
                    await asyncio.shield(task)
                except BaseException:
                    pass
            await self._mst_lifecycle.finish(emit=True)

        self._mst_teardown = asyncio.ensure_future(watch())
        # Errors from a user's on_file_output surface through stream_events();
        # retrieve them here so an abandoned stream doesn't warn instead.
        self._mst_teardown.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _await_teardown(self) -> None:
        if self._mst_teardown is None:
            await self._mst_lifecycle.finish(emit=True)
            return
        await asyncio.shield(self._mst_teardown)

    async def stream_events(self) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        try:
            await self._mst_lifecycle.prepare()
            async for event in super().stream_events():
                yield event
        except BaseException as err:
            setup_error = _unwrap(err)
            if setup_error is None:
                raise
            raise setup_error from None
        finally:
            await self._await_teardown()

    async def wait_completed(self) -> None:
        """Drive the run to completion without inspecting the events."""
        async for _ in self.stream_events():
            pass

    async def aclose(self) -> None:
        """Stop the run if it is still going, and wait for teardown to finish."""
        if not self.is_complete:
            self.cancel()
        await self._await_teardown()

    async def __aenter__(self) -> MstRunResultStreaming:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def run_streamed(
    agent: Agent[Any],
    input: Any,
    workspace: WorkspaceContext | None = None,
    **runner_kwargs: Any,
) -> MstRunResultStreaming:
    """Stream an agent run that has a microsandbox VM behind its sandbox tools.

    A drop-in for `agents.Runner.run_streamed()`. The sandbox is torn down when
    the stream finishes, so either iterate `stream_events()` or
    `await result.wait_completed()`.
    """
    lifecycle = _setup(agent, workspace, runner_kwargs)

    # Runner.run_streamed() starts its task here, and a task copies the current
    # context, so the provider has to be published before the call.
    token = set_provider(lifecycle)
    try:
        result = Runner.run_streamed(agent, input, **runner_kwargs)
    finally:
        reset_provider(token)

    # RunResultStreaming carries ~50 fields that only the SDK can fill in, so the
    # subclass is grafted onto the instance rather than reconstructing it.
    result.__class__ = MstRunResultStreaming
    streamed = cast(MstRunResultStreaming, result)
    streamed._mst_lifecycle = lifecycle
    streamed._arm_teardown()
    return streamed
