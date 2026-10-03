# Model Sandbox Tools (MST)
Microsandbox plugin for OpenAI Agents SDK. Give your agents isolated sandboxes for writing code.
MST wraps [microsandbox](https://github.com/superradcompany/microsandbox) and [@openai/agents](https://github.com/openai/openai-agents-js) so your agent can run Python scripts, read/write files, and execute shell commands, all inside a locked-down container.

## Packages

| Package | Description |
|---|---|
| `openai-microsandbox-core` | Sandbox lifecycle, workspace I/O, file change detection |
| `openai-agents-msb` | Drop-in `run()` replacement + sandbox tools for `@openai/agents` |

## Install

```bash
npm install openai-microsandbox-core openai-agents-msb @openai/agents microsandbox zod
```

`@openai/agents` and `zod` are peer dependencies of `openai-agents-msb`.

## Quick Start

The simplest way to use MST -> send files into a sandbox, let the agent work, get files back:

```ts
import fs from 'node:fs'
import { Agent } from '@openai/agents'
import { run, sandboxRun, sandboxReadFile, sandboxWriteFile, sandboxExec } from 'openai-agents-msb'
import { WorkspaceContext } from 'openai-microsandbox-core'

const agent = new Agent({
  name: 'doc-agent',
  instructions: 'You work with files in /workspace/. Use sandbox tools to read, write, and run code.',
  tools: [
    sandboxRun({ image: 'python:3.12-slim', interpreter: 'python3' }),
    sandboxReadFile(),
    sandboxWriteFile(),
    sandboxExec(),
  ],
})

const result = await run(
  agent,
  'Convert the spreadsheet to a summary PDF',
  WorkspaceContext({
    inputFiles: [
      { name: 'data.xlsx', data: fs.readFileSync('./data.xlsx') },
    ],
    onFileOutput: (payload) => {
      fs.copyFileSync(payload.path, `./output/${payload.file_name}`)
    },
  }),
)
```

MST automatically:
- Spins up an isolated microsandbox container
- Seeds `/workspace/` with your input files
- Runs the agent (which can call sandbox tools)
- Diffs the workspace after completion and calls `onFileOutput` for new/changed files
- Tears down the sandbox

The sandbox VM starts on the first sandbox tool call, so turns where the agent only chats boot nothing.
Agents reached through handoffs or `agent.asTool()` share the same sandbox. Tools without a `sandboxRun()`
anywhere in the agent graph use a default `python:3.12-slim` image.

## Sandbox Tools
MST provides five tools that agents can use inside the sandbox:

| Tool | What it does |
|---|---|
| `sandboxRun()` | Run a script (Python, etc.) inside the sandbox |
| `sandboxExec({ timeoutSecs? })` | Run a shell command (`pip install`, `ls`, etc.). Killed after `timeoutSecs` (default 600) |
| `sandboxReadFile()` | Read a text file from the sandbox filesystem |
| `sandboxWriteFile()` | Write a file to the sandbox filesystem |
| `sandboxListFiles()` | List files in the sandbox workspace |

### Configuring `sandboxRun`

```ts
sandboxRun({
  image: 'python:3.12-slim',   // Container image
  interpreter: 'python3',       // Script interpreter
  network: 'public',             // 'none' | 'public' | { allow: [...] } — see Network Security
  timeoutSecs: 120,              // Script timeout
  maxOutputBytes: 32768,         // stdout/stderr cap sent to the model (keeps head + tail)
  persist: true,                 // Keep sandbox alive between turns
  packages: ['python-docx'],    // Auto-install via pip
  memory: 512,                  // Memory limit (MB)
  secrets: [{                   // Inject secrets scoped to specific hosts
    env: 'API_KEY',
    value: process.env.API_KEY!,
    host: 'api.example.com',
  }],
})
```

## Workspace Context

`WorkspaceContext()` configures how files flow in and out of the sandbox.
`/workspace` is a host directory mounted into the VM, so files the agent wrote are still delivered if the VM crashes.
Persistent sessions keep theirs in `~/.mst/workspaces/<sandboxName>`; other runs use a temp directory that is deleted afterwards.

```ts
WorkspaceContext({
  // Files to seed into /workspace/ before the agent runs
  inputFiles: [
    { name: 'brief.docx', data: docxBuffer },
    new File([pdfBytes], 'contract.pdf'),
  ],

  // Called for each new or modified file after the agent finishes.
  // file_name is relative to /workspace, e.g. 'reports/q1/summary.md'.
  // payload.path is only valid during this callback; payload.buffer is omitted for files of 2 GiB or more.
  onFileOutput: (payload) => {
    console.log(`${payload.file_name} changed`)
    fs.copyFileSync(payload.path, payload.file_name)
  },

  // Called with the full file tree after each run (useful for UI)
  onWorkspaceSnapshot: (paths) => {
    console.log('Workspace:', paths)
  },

  // For persistent sandboxes: reuse across multiple turns
  sandboxName: 'my-session',
  skipInputSeed: true,  // Don't re-upload files on follow-up turns
})
```

## Streaming

MST's `run()` supports `@openai/agents` streaming. Pass `stream: true` and the sandbox stays alive until the stream completes:

```ts
const result = await run(agent, 'Draft a legal brief', {
  stream: true,
  ...WorkspaceContext({ inputFiles, onFileOutput }),
})

for await (const event of result) {
  if (event.type === 'raw_model_stream_event') {
    const data = event.data as { type?: string; delta?: string }
    if (data.type === 'output_text_delta') {
      process.stdout.write(data.delta ?? '')
    }
  }
}

await result.completed
```

## Network Security

Control what the sandbox can access:

```ts
// No network at all (the default when no packages/secrets are set); `false` also works
sandboxRun({ network: 'none', ... })

// Public internet only — LAN, host and cloud metadata addresses stay blocked; `true` also works
sandboxRun({ network: 'public', ... })

// Allowlist: exact domains, wildcards (also match the apex), IPs or CIDRs
sandboxRun({
  network: { allow: ['api.example.com', '*.githubusercontent.com', '10.0.0.0/8'] },
  ...
})

// Full control: pass a microsandbox NetworkPolicy straight through
import { NetworkPolicy } from 'microsandbox'
sandboxRun({ network: NetworkPolicy.fromProfiles(['public']), ... })
```

Hosts from `secrets[].host` and PyPI (when `packages` is set) are added to the allowlist automatically.
Domain rules and secrets make microsandbox terminate TLS on port 443 so it can match hostnames and substitute
secrets; code in the sandbox only ever sees a placeholder, never the real secret value.

## Persistent Sandboxes

Keep the sandbox alive across multiple `run()` calls in a conversation:

```ts
const workspace = {
  sandboxName: 'session-abc',
  inputFiles: [{ name: 'doc.docx', data: docxBuffer }],
  onFileOutput: (p) => { /* ... */ },
}

// First turn — seeds files
await run(agent, 'Summarize the document', WorkspaceContext(workspace))

// Follow-up turns — reuses the same sandbox, skips re-uploading
await run(agent, 'Now translate to Spanish', WorkspaceContext({
  ...workspace,
  skipInputSeed: true,
}))
```

Concurrent `run()` calls with the same `sandboxName` in one process are queued: each waits for the previous one to finish, so a second turn can never replace a sandbox that is still in use.

## Session Lifecycle

Persistent sessions keep a VM and a workspace folder on disk until you end them.

```ts
import { run, endSession, listSessions, SessionNotFoundError } from 'openai-agents-msb'

const workspaceRoot = '/secure/tenant-42'   // optional; default ~/.mst/workspaces

// Follow-up turn: re-seed if the session was deleted in the meantime
try {
  await run(agent, message, WorkspaceContext({ sandboxName, workspaceRoot, skipInputSeed: true, onFileOutput }))
} catch (err) {
  if (!(err instanceof SessionNotFoundError)) throw err
  await run(agent, message, WorkspaceContext({ sandboxName, workspaceRoot, inputFiles, onFileOutput }))
}

// Conversation closed: delete the VM and the files
await endSession(sandboxName, { workspaceRoot })

// Or keep the files (e.g. to archive them) and only delete the VM
await endSession(sandboxName, { workspaceRoot, keepFiles: true })

// Retention policy: end sessions idle for 30 days
for (const s of await listSessions({ workspaceRoot })) {
  if (Date.now() - s.lastUsed.getTime() > 30 * 24 * 3600 * 1000) await endSession(s.name, { workspaceRoot })
}
```

- `endSession()` waits for any in-flight `run()` on that session, and is a no-op for unknown names.
- `listSessions()` returns `{ name, workspaceDir, sandbox: 'running' | 'stopped' | 'missing', lastUsed }`, newest first.
- Pass the same `workspaceRoot` to every turn of a session and to `listSessions()` / `endSession()`.
- Starting a session again with `skipInputSeed: false` clears its workspace, so copy out files kept with `keepFiles` first.

## Example: Law Firm Document Agent

The `examples/law-firm/` directory contains a full working demo — a legal document assistant that processes `.docx` templates and case notes inside a sandbox, with a terminal UI built on [OpenTUI](https://opentui.com/).

```bash
# Run the headless version
pnpm demo:law-firm

# Run the interactive TUI
pnpm demo:law-firm:cli
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your App                                       │
│  ┌───────────────────────────────────────────┐  │
│  │  @openai/agents  ←  Agent + tools         │  │
│  └────────────┬──────────────────────────────┘  │
│               │                                  │
│  ┌────────────▼──────────────────────────────┐  │
│  │  openai-agents-msb                        │  │
│  │  run() · sandboxRun · sandboxExec · ...   │  │
│  └────────────┬──────────────────────────────┘  │
│               │                                  │
│  ┌────────────▼──────────────────────────────┐  │
│  │  openai-microsandbox-core                 │  │
│  │  WorkspaceContext · sandbox lifecycle     │  │
│  │  file snapshots · change detection        │  │
│  └────────────┬──────────────────────────────┘  │
│               │                                  │
│  ┌────────────▼──────────────────────────────┐  │
│  │  microsandbox                             │  │
│  │  Isolated container · fs · shell · net    │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

## License

MIT
