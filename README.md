# openai-agents-msb

**Give your OpenAI Agents an isolated [microsandbox](https://github.com/superradcompany/microsandbox) VM to run code and edit files in [TypeScript](packages/openai-agents) or [Python](python).**

- **Drop-in `run()` method**: Same arguments as the Agents SDK's own runner. Swap the call, keep the agent definition from the SDK.
- **Two SDKs, one behaviour**: TypeScript and Python, same tools, same guarantees.
- **Files in, files out**: Seed `/workspace`, get every new or changed file back after the run.
- **Lazy VMs**: The sandbox boots on the first tool call. Chat-only turns cost nothing.
- **Locked-down network**: No network by default; allow the public internet or specific hosts.
- **Secrets that stay out**: Code in the VM sees a placeholder, never the real key.
- **Persistent sessions**: Keep one VM per conversation across turns.

&nbsp;

## &nbsp; Getting Started

#### &nbsp; Install the SDK

> ```sh
> npm i openai-agents-msb @openai/agents microsandbox zod # 🟦 TypeScript
> ```
>
> ```sh
> pip install openai-agents-msb # 🐍 Python
> ```

##

> **Requirements**:
>
> - **macOS**: Apple Silicon.
> - **Linux**: KVM enabled.
> - **Windows**: WHP enabled.
>
> Plus Node.js 22+ or Python 3.10+. The microsandbox runtime ships with the SDK; no daemon to run.

#### &nbsp; Give an agent a sandbox

> ```ts
> import fs from 'node:fs'
> import { Agent } from '@openai/agents'
> import { run, sandboxConfig, sandboxExec, sandboxReadFile, sandboxWriteFile } from 'openai-agents-msb'
> import { WorkspaceContext } from 'openai-agents-msb/core'
>
> const agent = new Agent({
>   name: 'doc-agent',
>   instructions: 'Files are in /workspace. Use the sandbox tools.',
>   tools: [
>     sandboxConfig({ image: 'python:3.12-slim', interpreter: 'python3' }),
>     sandboxExec(),
>     sandboxReadFile(),
>     sandboxWriteFile(),
>   ],
> })
>
> await run(agent, 'Summarize data.xlsx as a PDF', WorkspaceContext({
>   inputFiles: [{ name: 'data.xlsx', data: fs.readFileSync('data.xlsx') }],
>   onFileOutput: (file) => fs.copyFileSync(file.path, `out/${file.file_name}`),
> }))
> ```

> ```python
> import asyncio
> import shutil
> from pathlib import Path
>
> from agents import Agent
> from openai_agents_msb import (
>     InputFile, WorkspaceContext, run,
>     sandbox_config, sandbox_exec, sandbox_read_file, sandbox_write_file,
> )
>
> agent = Agent(
>     name="doc-agent",
>     instructions="Files are in /workspace. Use the sandbox tools.",
>     tools=[
>         sandbox_config(image="python:3.12-slim", interpreter="python3"),
>         sandbox_exec(),
>         sandbox_read_file(),
>         sandbox_write_file(),
>     ],
> )
>
> async def main() -> None:
>     await run(agent, "Summarize data.xlsx as a PDF", WorkspaceContext(
>         input_files=[InputFile("data.xlsx", Path("data.xlsx").read_bytes())],
>         on_file_output=lambda f: shutil.copy(f.path, f"out/{f.file_name}"),
>     ))
>
> asyncio.run(main())
> ```

##

> The workspace is the optional third argument; everything else is passed through to the underlying
> runner unchanged. Python also has `run_sync()`, mirroring `Runner.run_sync()`.

&nbsp;

## &nbsp; Tools

| Does | TypeScript | Python |
|---|---|---|
| Runs a script; configures the sandbox | `sandboxConfig(options)` | `sandbox_config(**options)` |
| Runs a shell command (default timeout 600s) | `sandboxExec({ timeoutSecs?, maxOutputBytes? })` | `sandbox_exec(timeout_secs=600, max_output_bytes=32768)` |
| Reads a text file | `sandboxReadFile()` | `sandbox_read_file()` |
| Writes a text file | `sandboxWriteFile()` | `sandbox_write_file()` |
| Lists a directory | `sandboxListFiles()` | `sandbox_list_files()` |

The model sees the same five tool names in both languages: `sandbox_run`, `sandbox_exec`,
`sandbox_read_file`, `sandbox_write_file` and `sandbox_list_files`.

<details>
<summary><em>All sandbox options →</em></summary>

##

```ts
sandboxConfig({
  image: 'python:3.12-slim',
  interpreter: 'python3',
  network: 'public',           // see Network
  packages: ['python-docx'],   // pip install on start
  timeoutSecs: 120,            // per script, default 30
  maxOutputBytes: 32768,       // stdout/stderr sent to the model
  memory: 512,                 // MiB
  cpus: 1,
  persist: true,               // see Sessions
  secrets: [{ env: 'API_KEY', value: process.env.API_KEY!, host: 'api.example.com' }],
  env: { TZ: 'UTC' },
  configure: (builder) => builder,   // escape hatch to the microsandbox builder
})
```

```python
sandbox_config(
    image="python:3.12-slim",
    interpreter="python3",
    network="public",                  # see Network
    packages=["python-docx"],          # pip install on start
    timeout_secs=120,                  # per script, default 30
    max_output_bytes=32768,            # stdout/stderr sent to the model
    memory=512,                        # MiB
    cpus=1,
    persist=True,                      # see Sessions
    secrets=[SandboxSecret(env="API_KEY", value=key, host="api.example.com")],
    env={"TZ": "UTC"},
    create_kwargs={},                  # escape hatch to microsandbox Sandbox.create()
)
```

Handoffs and `asTool()` / `as_tool()` agents share the run's sandbox. Agents with no sandbox config
anywhere use `python:3.12-slim`. Every reachable config must match, or the run fails before the
model is called.

</details>

&nbsp;

## &nbsp; Files

The file output callback fires for each new or changed file after the run. `file_name` is relative to
`/workspace`, the host `path` is valid for the duration of the callback, and the in-memory contents
are omitted for files of 2 GiB or more.

> ```ts
> WorkspaceContext({
>   inputFiles: [{ name: 'brief.docx', data }, new File([pdf], 'contract.pdf')],
>   onFileOutput: (file) => fs.copyFileSync(file.path, file.file_name),
>   onWorkspaceSnapshot: (paths) => console.log(paths),
> })
> ```

> ```python
> WorkspaceContext(
>     input_files=[InputFile("brief.docx", data), Path("contract.pdf")],
>     on_file_output=lambda f: shutil.copy(f.path, f.file_name),
>     on_workspace_snapshot=lambda paths: print(paths),
> )
> ```
>
> Both Python callbacks may be sync or async.

&nbsp;

## &nbsp; Streaming

> ```ts
> const result = await run(agent, 'Draft a brief', { stream: true, ...WorkspaceContext({ onFileOutput }) })
> for await (const event of result) { /* ... */ }
> await result.completed   // outputs delivered, sandbox stopped
> ```

> ```python
> result = run_streamed(agent, "Draft a brief", WorkspaceContext(on_file_output=save))
> async for event in result.stream_events():
>     ...
> # outputs delivered and the VM stopped once the stream ends
> ```
>
> Python also offers `await result.wait_completed()` when you don't want the events, and
> `async with run_streamed(...) as result:` to close everything down deterministically.

##

> Teardown runs when the stream finishes, including when you break out early or the run is cancelled.

&nbsp;

## &nbsp; Network

> ```ts
> sandboxConfig({ network: 'none' })                                // default
> sandboxConfig({ network: 'public' })                              // internet, not LAN or cloud metadata
> sandboxConfig({ network: { allow: ['api.example.com', '*.github.com', '10.0.0.0/8'] } })
> ```

> ```python
> sandbox_config(..., network="none")      # default
> sandbox_config(..., network="public")    # internet, not LAN or cloud metadata
> sandbox_config(..., network=Allow(["api.example.com", "*.github.com", "10.0.0.0/8"]))
> ```

##

> `secrets` hosts and PyPI (for `packages`) are allowed automatically. Invalid policies fail before
> the model runs. A host that echoes request headers back can leak a secret's real value, so only
> scope secrets to APIs you trust.

&nbsp;

## &nbsp; Sessions

Turn on `persist` and pass a sandbox name to keep one VM per conversation.

> ```ts
> await run(agent, 'Draft it', WorkspaceContext({ sandboxName: 'case-42', inputFiles }))
> await run(agent, 'Now shorten it', WorkspaceContext({ sandboxName: 'case-42', skipInputSeed: true }))
>
> await endSession('case-42')        // delete the VM and its files
> await listSessions()               // [{ name, sandbox, lastUsed, workspaceDir }]
> ```

> ```python
> await run(agent, "Draft it", WorkspaceContext(sandbox_name="case-42", input_files=files))
> await run(agent, "Now shorten it", WorkspaceContext(sandbox_name="case-42", skip_input_seed=True))
>
> await end_session("case-42")   # delete the VM and its files
> await list_sessions()          # [SessionInfo(name, workspace_dir, sandbox, last_used)]
> ```

##

> A follow-up on a deleted session raises `SessionNotFoundError`. Runs on the same session are queued.
> Session workspaces live under `~/.mst/workspaces` unless you set a workspace root.

&nbsp;

## &nbsp; Packages

| | |
|---|---|
| [`openai-agents-msb`](packages/openai-agents) (npm) | TypeScript, for [`@openai/agents`](https://github.com/openai/openai-agents-js) |
| [`openai-agents-msb`](python) (PyPI) | Python, for [`openai-agents`](https://github.com/openai/openai-agents-python) |

##

> Releasing the Python package:
>
> ```sh
> scripts/publish-python.sh --dry-run   # build + verify, upload nothing
> scripts/publish-python.sh --test      # TestPyPI
> scripts/publish-python.sh             # PyPI
> ```
>
> Needs `UV_PUBLISH_TOKEN`, or `--trusted-publishing always` in CI. Run with `--help` for the
> full list of checks.

&nbsp;

## &nbsp; License

MIT
