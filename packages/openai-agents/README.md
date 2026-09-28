# @iyifr/openai-agents-msb

Give [`@openai/agents`](https://github.com/openai/openai-agents-js) agents an isolated [microsandbox](https://github.com/superradcompany/microsandbox) workspace: a drop-in `run()` replacement plus five sandbox tools.

Part of [Model Sandbox Tools (MST)](https://github.com/iyifr/model-sandbox-tools).

## Install

```bash
npm install mst-core @iyifr/openai-agents-msb @openai/agents microsandbox zod
```

`@openai/agents` and `zod` are peer dependencies.

## Quick start

```ts
import fs from 'node:fs'
import { Agent } from '@openai/agents'
import { run, sandboxRun, sandboxReadFile, sandboxWriteFile, sandboxExec } from '@iyifr/openai-agents-msb'
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
    inputFiles: [{ name: 'data.xlsx', data: fs.readFileSync('./data.xlsx') }],
    onFileOutput: (payload) => fs.writeFileSync(`./output/${payload.file_name}`, payload.buffer),
  }),
)
```

`run()` spins up the sandbox, seeds `/workspace/`, runs the agent, diffs the workspace, emits changed files, and tears the sandbox down.

## Tools

| Tool | What it does |
|---|---|
| `sandboxRun()` | Run a script (Python, etc.) inside the sandbox |
| `sandboxExec()` | Run a shell command (`pip install`, `ls`, etc.) |
| `sandboxReadFile()` | Read a text file from the sandbox filesystem |
| `sandboxWriteFile()` | Write a file to the sandbox filesystem |
| `sandboxListFiles()` | List files in the sandbox workspace |

Streaming, persistent sandboxes, network allowlists and scoped secrets are covered in the [main README](https://github.com/iyifr/model-sandbox-tools#readme).

## License

MIT
