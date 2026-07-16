# Model Sandbox Tools (MST)

Give AI agents isolated sandboxes to work with documents. MST wraps [microsandbox](https://github.com/superradcompany/microsandbox) and [@openai/agents](https://github.com/openai/openai-agents-js) so your agent can run Python scripts, read/write files, and execute shell commands — all inside a locked-down container.

## Packages

| Package | Description |
|---|---|
| `mst-core` | Sandbox lifecycle, workspace I/O, file change detection |
| `@mst/openai-agents` | Drop-in `run()` replacement + sandbox tools for `@openai/agents` |

## Install

```bash
npm install mst-core @mst/openai-agents @openai/agents microsandbox
```

## Quick Start

The simplest way to use MST — send files into a sandbox, let the agent work, get files back:

```ts
import fs from 'node:fs'
import { Agent } from '@openai/agents'
import { run, sandboxRun, sandboxReadFile, sandboxWriteFile, sandboxExec } from '@mst/openai-agents'
import { WorkspaceContext } from 'mst-core'

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
      fs.writeFileSync(`./output/${payload.file_name}`, payload.buffer)
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

## Sandbox Tools

MST provides five tools that agents can use inside the sandbox:

| Tool | What it does |
|---|---|
| `sandboxRun()` | Run a script (Python, etc.) inside the sandbox |
| `sandboxExec()` | Run a shell command (`pip install`, `ls`, etc.) |
| `sandboxReadFile()` | Read a text file from the sandbox filesystem |
| `sandboxWriteFile()` | Write a file to the sandbox filesystem |
| `sandboxListFiles()` | List files in the sandbox workspace |

### Configuring `sandboxRun`

```ts
sandboxRun({
  image: 'python:3.12-slim',   // Container image
  interpreter: 'python3',       // Script interpreter
  network: true,                 // Allow network access (or { allow: ['api.example.com'] })
  timeoutSecs: 120,              // Script timeout
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

`WorkspaceContext()` configures how files flow in and out of the sandbox:

```ts
WorkspaceContext({
  // Files to seed into /workspace/ before the agent runs
  inputFiles: [
    { name: 'brief.docx', data: docxBuffer },
    new File([pdfBytes], 'contract.pdf'),
  ],

  // Called for each new or modified file after the agent finishes
  onFileOutput: (payload) => {
    console.log(`${payload.file_name} changed`)
    fs.writeFileSync(payload.file_name, payload.buffer)
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
// No network at all (default if no packages/secrets)
sandboxRun({ network: false, ... })

// Full unrestricted network
sandboxRun({ network: true, ... })

// Allowlist specific domains
sandboxRun({
  network: { allow: ['api.example.com', 'cdn.example.com'] },
  ...
})
```

Domains from `secrets[].host` and PyPI (when `packages` is set) are automatically added to the allowlist.

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
│  │  @mst/openai-agents                       │  │
│  │  run() · sandboxRun · sandboxExec · ...   │  │
│  └────────────┬──────────────────────────────┘  │
│               │                                  │
│  ┌────────────▼──────────────────────────────┐  │
│  │  mst-core                                 │  │
│  │  WorkspaceContext · sandbox lifecycle      │  │
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
