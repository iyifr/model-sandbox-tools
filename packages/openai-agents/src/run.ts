import { run as openaiRun, type Agent } from '@openai/agents'

import { Sandbox, Volume } from 'microsandbox'
import {
  sandboxStore,
  WORKSPACE_CTX,
  assertSafeFilename,
  normalizeFile,
} from 'mst-core'
import type { WorkspaceContextOptions } from 'mst-core'
import { discoverSandboxConfig } from './discover-config.js'

function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  return Buffer.from(a).equals(Buffer.from(b))
}

type RunOptions = Record<string, unknown> & {
  [WORKSPACE_CTX]?: WorkspaceContextOptions
}

export async function run(
  agent: Agent<any, any>,
  input: string,
  options?: RunOptions,
) {
  if (options != null && 'sandbox' in options && options.sandbox !== undefined) {
    throw new Error(
      '[mst] `options.sandbox` is reserved by @openai/agents for its built-in sandbox ' +
        '(shell/apply_patch). MST manages microsandbox via sandboxRun() tools. ' +
        'Remove options.sandbox, or import { run } from "@openai/agents" directly ' +
        'if you need the SDK sandbox instead of MST.',
    )
  }

  const { [WORKSPACE_CTX]: workspace, ...openaiOptions } = options ?? {}

  const config = discoverSandboxConfig(agent)
  if (!config) {
    return openaiRun(agent, input, openaiOptions as Parameters<typeof openaiRun>[2])
  }

  const name = `mst-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`

  let builder = Sandbox.builder(name)
    .image(config.image)
    .cpus(config.cpus ?? 1)
    .memory(config.memory ?? 256)
    .ephemeral(true)
    .replace()

  if (!config.network) builder = builder.disableNetwork()
  if (config.env) {
    for (const [k, v] of Object.entries(config.env)) {
      builder = builder.env(k, v)
    }
  }

  for (const vol of config.volumes ?? []) {
    const createIfMissing = vol.createIfMissing ?? true
    try {
      await Volume.get(vol.name)
    } catch {
      if (createIfMissing) {
        await Volume.builder(vol.name).create()
      } else {
        throw new Error(`[mst] Volume not found: ${vol.name}`)
      }
    }
    builder = builder.volume(vol.mountPath, (m) => {
      let mount = m.named(vol.name)
      if (vol.readonly) mount = mount.readonly()
      return mount
    })
  }

  if (config.configure) builder = config.configure(builder)

  const sb = await builder.create()
  const inputSnapshot = new Map<string, Uint8Array>()

  try {
    const ws = workspace as WorkspaceContextOptions | undefined
    if (ws?.inputFiles?.length) {
      await sb.fs().mkdir('/workspace').catch(() => {})
      for (const f of ws.inputFiles) {
        const fileName = f.name
        assertSafeFilename(fileName)
        const data =
          f instanceof File
            ? new Uint8Array(await f.arrayBuffer())
            : normalizeFile(f).data
        await sb.fs().write(`/workspace/${fileName}`, data)
        inputSnapshot.set(fileName, data)
      }
    }

    const toolExecution = (openaiOptions.toolExecution ?? {}) as {
      maxFunctionToolConcurrency?: number | null
    }
    openaiOptions.toolExecution = {
      ...toolExecution,
      maxFunctionToolConcurrency:
        toolExecution.maxFunctionToolConcurrency ?? 1,
    }

    const result = await sandboxStore.run(sb, () =>
      openaiRun(agent, input, openaiOptions as Parameters<typeof openaiRun>[2]),
    )

    if (ws?.onFileOutput) {
      const entries = await sb.fs().list('/workspace').catch(() => [])
      for (const entry of entries) {
        if (entry.kind !== 'file') continue
        const fileName = entry.path.split('/').pop()!
        const bytes = await sb.fs().read(entry.path)
        const original = inputSnapshot.get(fileName)
        const changed = original
          ? !bytesEqual(original, bytes)
          : true
        if (!changed) continue
        await ws.onFileOutput({
          file_name: fileName,
          version: 1,
          buffer: bytes,
        })
      }
    }

    return result
  } finally {
    await sb.stop()
  }
}
