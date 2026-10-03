# @iyifr/mst-core

Core primitives for [Model Sandbox Tools (MST)](https://github.com/iyifr/model-sandbox-tools) — sandbox lifecycle, workspace I/O, and file change detection on top of [microsandbox](https://github.com/superradcompany/microsandbox).

Most users don't install this directly. If you're using the OpenAI Agents SDK, install [`@iyifr/openai-agents-msb`](https://www.npmjs.com/package/@iyifr/openai-agents-msb), which depends on this package.

## Install

```bash
npm install @iyifr/mst-core microsandbox
```

## What's in here

- `WorkspaceContext()` — declares the files that flow into and out of a sandbox
- Sandbox lifecycle helpers and a sandbox store for persistent sessions
- Workspace snapshotting and diffing, so new/changed files can be handed back after a run

## Usage

```ts
import { WorkspaceContext } from '@iyifr/mst-core'

const ctx = WorkspaceContext({
  inputFiles: [{ name: 'data.xlsx', data: buf }],
  onFileOutput: (payload) => fs.copyFileSync(payload.path, payload.file_name),
})
```

See the [main README](https://github.com/iyifr/model-sandbox-tools#readme) for the full guide.

## License

MIT
