"""Shared fixtures: a scripted model, and a fake VM that writes to the real bind mount.

The fake sandbox stands in for microsandbox so the lifecycle, workspace diffing
and streaming teardown can be tested without booting a VM. Tests that need a real
VM are marked `vm`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from agents import Agent, FunctionTool
from agents.testing import ScriptedModel
from agents.testing.model import assistant_message, function_call
from microsandbox import ExecEventType, FsEntryKind, Sandbox, SandboxStatus

import openai_agents_msb._run as run_module
from openai_agents_msb import SandboxConfig

GUEST_WORKSPACE = "/workspace"


# -- scripted model helpers ------------------------------------------------------------


def scripted(*steps: Any) -> ScriptedModel:
    """A model that emits each step's items in order. Steps are lists of output items."""
    return ScriptedModel(list(steps))


def calls_tool(name: str, args: dict[str, Any], *, call_id: str = "call-1") -> list[Any]:
    return [function_call(name, args, call_id=call_id)]


def says(text: str) -> list[Any]:
    return [assistant_message(text)]


def agent_calling(
    tools: Sequence[FunctionTool],
    *steps: Any,
    name: str = "test-agent",
    handoffs: Sequence[Agent[Any]] = (),
) -> Agent[Any]:
    return Agent(
        name=name,
        instructions="Use the sandbox tools.",
        tools=list(tools),
        handoffs=list(handoffs),
        model=scripted(*steps),
    )


# -- fake sandbox ----------------------------------------------------------------------


@dataclass
class _Event:
    event_type: ExecEventType
    data: bytes | None = None
    code: int | None = None
    pid: int | None = None


@dataclass
class _Entry:
    path: str
    kind: FsEntryKind
    size: int = 0
    mode: int = 0o644
    modified: float = 0.0


class _FakeMetadata:
    def __init__(self, size: int, kind: FsEntryKind) -> None:
        self.size = size
        self.kind = kind


class FakeFs:
    """Guest filesystem backed by the host. `/workspace` is the real bind mount."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sb = sandbox

    def _host(self, path: str) -> Path:
        if path == GUEST_WORKSPACE or path.startswith(f"{GUEST_WORKSPACE}/"):
            rel = path[len(GUEST_WORKSPACE) :].lstrip("/")
            return self._sb.host_dir / rel if rel else self._sb.host_dir
        return self._sb.guest_root / path.lstrip("/")

    async def write(self, path: str, data: bytes) -> None:
        target = self._host(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._sb.writes.append(path)

    async def read(self, path: str) -> bytes:
        return self._host(path).read_bytes()

    async def read_text(self, path: str) -> str:
        return self._host(path).read_text()

    async def stat(self, path: str) -> _FakeMetadata:
        target = self._host(path)
        stat = target.stat()
        kind = FsEntryKind.DIRECTORY if target.is_dir() else FsEntryKind.FILE
        return _FakeMetadata(stat.st_size, kind)

    async def list(self, path: str) -> list[_Entry]:
        target = self._host(path)
        return [
            _Entry(
                path=f"{path.rstrip('/')}/{child.name}",
                kind=FsEntryKind.DIRECTORY if child.is_dir() else FsEntryKind.FILE,
                size=child.stat().st_size if child.is_file() else 0,
            )
            for child in sorted(target.iterdir())
        ]

    async def mkdir(self, path: str) -> None:
        self._host(path).mkdir(parents=True, exist_ok=True)

    async def remove(self, path: str) -> None:
        self._host(path).unlink(missing_ok=True)
        self._sb.removes.append(path)


class FakeExecHandle:
    def __init__(self, sandbox: FakeSandbox, cmd: str, args: list[str]) -> None:
        self._sb = sandbox
        self._cmd = cmd
        self._args = args
        self._events = sandbox.exec_behaviour(cmd, args)
        self._index = 0
        self._killed = asyncio.Event()

    def __aiter__(self) -> FakeExecHandle:
        return self

    async def __anext__(self) -> _Event:
        if self._killed.is_set():
            raise StopAsyncIteration
        if self._sb.exec_hangs:
            # A real handle ends its event stream when killed, so block until then.
            await self._killed.wait()
            raise StopAsyncIteration
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event

    @property
    def killed(self) -> bool:
        return self._killed.is_set()

    async def kill(self) -> None:
        if not self._killed.is_set():
            self._sb.kills += 1
        self._killed.set()


@dataclass
class ShellResult:
    success: bool = True
    exit_code: int = 0
    stdout_text: str = ""
    stderr_text: str = ""


@dataclass
class FakeSandbox:
    """A stand-in for `microsandbox.Sandbox`."""

    name: str
    config: SandboxConfig
    host_dir: Path
    guest_root: Path

    recorder: SandboxRecorder | None = None
    status: SandboxStatus = SandboxStatus.STOPPED
    stopped: int = 0
    kills: int = 0
    exec_hangs: bool = False
    writes: list[str] = field(default_factory=list)
    removes: list[str] = field(default_factory=list)
    execs: list[tuple[str, list[str]]] = field(default_factory=list)
    shells: list[str] = field(default_factory=list)
    shell_result: ShellResult = field(default_factory=ShellResult)
    script_output: bytes = b"ok\n"
    exit_code: int = 0

    @property
    def fs(self) -> FakeFs:
        return FakeFs(self)

    def exec_behaviour(self, cmd: str, args: list[str]) -> list[_Event]:
        return [
            _Event(ExecEventType.STARTED, pid=1),
            _Event(ExecEventType.STDOUT, data=self.script_output),
            _Event(ExecEventType.EXITED, code=self.exit_code),
        ]

    async def exec_stream(self, cmd: str, args: list[str] | None = None) -> FakeExecHandle:
        self.execs.append((cmd, list(args or [])))
        return FakeExecHandle(self, cmd, list(args or []))

    async def shell(self, script: str, **_: Any) -> ShellResult:
        self.shells.append(script)
        return self.shell_result

    async def stop(self, timeout: float | None = None) -> None:
        self.stopped += 1
        if self.recorder is not None:
            self.recorder.live -= 1


@dataclass
class SandboxRecorder:
    """Every fake VM a test created, in creation order."""

    created: list[FakeSandbox] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    live: int = 0
    """VMs booted but not yet stopped."""
    peak_live: int = 0
    """The most VMs alive at once, for asserting that runs were serialized."""

    @property
    def only(self) -> FakeSandbox:
        assert len(self.created) == 1, f"expected exactly one sandbox, got {len(self.created)}"
        return self.created[0]


@pytest.fixture
def sandboxes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SandboxRecorder]:
    """Replace VM creation with `FakeSandbox`, and record what was created."""
    recorder = SandboxRecorder()
    guest_root = tmp_path / "guest"
    guest_root.mkdir()

    async def fake_create(
        name: str, config: SandboxConfig, host_dir: Path, persistent: bool
    ) -> FakeSandbox:
        sb = FakeSandbox(
            name=name,
            config=config,
            host_dir=host_dir,
            guest_root=guest_root,
            recorder=recorder,
        )
        recorder.created.append(sb)
        recorder.live += 1
        recorder.peak_live = max(recorder.peak_live, recorder.live)
        return sb

    async def fake_remove(name: str) -> None:
        recorder.removed.append(name)

    monkeypatch.setattr(run_module, "_create_sandbox", fake_create)
    monkeypatch.setattr(Sandbox, "remove", fake_remove)
    yield recorder


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """An isolated session root, so tests never touch ~/.mst."""
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")


def host_file(sb: FakeSandbox, rel: str) -> Path:
    return sb.host_dir / rel


def assert_no_leftovers(root: Path) -> None:
    """No temp workspace should survive a run."""
    leftovers = [p.name for p in root.glob("**/mst-ws-*")] if root.exists() else []
    assert leftovers == [], f"leftover temp workspaces: {leftovers}"


def guest_path(rel: str) -> str:
    return f"{GUEST_WORKSPACE}/{rel}"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}
