# openai-agents-msb

Give [`@openai/agents`](https://github.com/openai/openai-agents-js) agents an isolated [microsandbox](https://github.com/superradcompany/microsandbox) workspace: a drop-in `run()` replacement plus five sandbox tools.

Part of [Model Sandbox Tools (MST)](https://github.com/iyifr/model-sandbox-tools).

## Install

```bash
npm install openai-microsandbox-core openai-agents-msb @openai/agents microsandbox zod
```

`@openai/agents` and `zod` are peer dependencies.

## Quick start

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
    inputFiles: [{ name: 'data.xlsx', data: fs.readFileSync('./data.xlsx') }],
    onFileOutput: (payload) => fs.copyFileSync(payload.path, `./output/${payload.file_name}`),
  }),
)
```

`run()` has the same signatures as `run()` from `@openai/agents`. On the first sandbox tool call it starts the sandbox
and seeds `/workspace/`; after the run it emits changed files and tears the sandbox down. MST does not change
`@openai/agents` tracing; call `setTracingDisabled(true)` yourself if your model provider has no OpenAI API key.

## Tools

| Tool | What it does |
|---|---|
| `sandboxRun()` | Run a script (Python, etc.) inside the sandbox |
| `sandboxExec({ timeoutSecs? })` | Run a shell command (`pip install`, `ls`, etc.). Killed after `timeoutSecs` (default 600) |
| `sandboxReadFile()` | Read a text file from the sandbox filesystem |
| `sandboxWriteFile()` | Write a file to the sandbox filesystem |
| `sandboxListFiles()` | List files in the sandbox workspace |

Streaming, persistent sessions (`endSession`, `listSessions`), network allowlists and scoped secrets are covered in the [main README](https://github.com/iyifr/model-sandbox-tools#readme).

## License

MIT
