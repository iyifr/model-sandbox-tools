import { createHash } from 'node:crypto'
import { run as openaiRun, type Agent } from '@openai/agents'

import { Sandbox, Volume } from 'microsandbox'
import type { Sandbox as SandboxInstance } from 'microsandbox'
import {
  sandboxStore,
  bindStreamSandbox,
  unbindStreamSandbox,
  WORKSPACE_CTX,
  assertSafeFilename,
  normalizeFile,
} from 'mst-core'
import type { WorkspaceContextOptions } from 'mst-core'
import { discoverSandboxConfig } from './discover-config.js'

type FileFingerprint = { size: number; modified: number; hash: string }

function hashBytes(data: Uint8Array): string {
  return createHash('sha256').update(data).digest('hex')
}

type RunOptions = Record<string, unknown> & {
  [WORKSPACE_CTX]?: WorkspaceContextOptions
}

async function snapshotWorkspace(
  sb: SandboxInstance,
): Promise<Map<string, FileFingerprint>> {
  const snapshot = new Map<string, FileFingerprint>()
  const entries = await sb.fs().list('/workspace').catch(() => [])
  for (const entry of entries) {
    if (entry.kind !== 'file') continue
    const fileName = entry.path.split('/').pop()!
    const data = await sb.fs().read(entry.path)
    snapshot.set(fileName, {
      size: entry.size,
      modified: entry.modified?.getTime() ?? 0,
      hash: hashBytes(data),
    })
  }
  return snapshot
}

function snapshotFromInputFiles(
  files: Array<{ name: string; data: Uint8Array }>,
): Map<string, FileFingerprint> {
  const snapshot = new Map<string, FileFingerprint>()
  for (const f of files) {
    snapshot.set(f.name, {
      size: f.data.byteLength,
      modified: 0,
      hash: hashBytes(f.data),
    })
  }
  return snapshot
}

async function listWorkspacePaths(
  sb: SandboxInstance,
  dir = '/workspace',
): Promise<string[]> {
  const paths: string[] = []
  const entries = await sb.fs().list(dir).catch(() => [])
  for (const entry of entries) {
    paths.push(entry.path)
    if (entry.kind === 'directory') {
      paths.push(...(await listWorkspacePaths(sb, entry.path)))
    }
  }
  return paths.sort()
}

async function notifyWorkspaceSnapshot(
  sb: SandboxInstance,
  ws: WorkspaceContextOptions | undefined,
) {
  if (!ws?.onWorkspaceSnapshot) return
  await ws.onWorkspaceSnapshot(await listWorkspacePaths(sb))
}

async function emitFileOutputs(
  sb: SandboxInstance,
  ws: WorkspaceContextOptions | undefined,
  inputSnapshot: Map<string, FileFingerprint>,
) {
  await notifyWorkspaceSnapshot(sb, ws)

  if (!ws?.onFileOutput) return

  const entries = await sb.fs().list('/workspace').catch(() => [])
  for (const entry of entries) {
    if (entry.kind !== 'file') continue
    const fileName = entry.path.split('/').pop()!
    const original = inputSnapshot.get(fileName)

    // Fast path: skip files whose size and mtime haven't changed
    if (original) {
      const sameSize = entry.size === original.size
      const sameMtime =
        entry.modified != null && entry.modified.getTime() === original.modified
      if (sameSize && sameMtime) continue
    }

    // Slow path: read and hash to confirm change
    const bytes = await sb.fs().read(entry.path)
    if (original) {
      const hash = hashBytes(bytes)
      if (hash === original.hash) continue
    }

    await ws.onFileOutput({
      file_name: fileName,
      version: 1,
      buffer: bytes,
    })
  }
}

function attachStreamSandboxLifecycle(
  sb: SandboxInstance,
  result: { completed: Promise<void> },
  ws: WorkspaceContextOptions | undefined,
  inputSnapshot: Map<string, FileFingerprint>,
) {
  const teardown = result.completed.finally(async () => {
    try {
      await emitFileOutputs(sb, ws, inputSnapshot)
    } finally {
      unbindStreamSandbox(sb)
      await sb.stop()
    }
  })

  Object.assign(result, { mstTeardown: teardown })
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
  const streaming = openaiOptions.stream === true

  const config = discoverSandboxConfig(agent)
  if (!config) {
    return openaiRun(agent, input, openaiOptions as Parameters<typeof openaiRun>[2])
  }

  if (config.network === false) {
    const hasPackages = (config.packages?.length ?? 0) > 0
    const hasSecrets = (config.secrets?.length ?? 0) > 0
    if (hasPackages || hasSecrets) {
      throw new Error(
        '[mst] network: false is incompatible with `packages` and `secrets` — ' +
          'they require network access. Use network: { allow: [] } to enable an allowlist.',
      )
    }
  }

  const ws = workspace as WorkspaceContextOptions | undefined
  const name = ws?.sandboxName ?? `mst-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`
  const reusingPersistedSandbox =
    config.persist === true && ws?.sandboxName != null && ws.skipInputSeed === true

  const sb = reusingPersistedSandbox
    ? await Sandbox.start(name)
    : await createSandbox(name, config)

  let inputSnapshot = new Map<string, FileFingerprint>()

  try {
    if (!reusingPersistedSandbox && config.packages?.length) {
      const out = await sb.shell(`pip install --quiet ${config.packages.join(' ')}`)
      if (!out.success) {
        throw new Error(
          `[mst] pip install failed (exit ${out.code}):\n${out.stderr()}`,
        )
      }
    }

    if (ws?.inputFiles?.length && !ws.skipInputSeed) {
      await sb.fs().mkdir('/workspace').catch(() => {})
      const seeded: Array<{ name: string; data: Uint8Array }> = []
      for (const f of ws.inputFiles) {
        const fileName = f.name
        assertSafeFilename(fileName)
        const data =
          f instanceof File
            ? new Uint8Array(await f.arrayBuffer())
            : normalizeFile(f).data
        await sb.fs().write(`/workspace/${fileName}`, data)
        seeded.push({ name: fileName, data })
      }
      inputSnapshot = snapshotFromInputFiles(seeded)
    } else if (ws?.skipInputSeed) {
      inputSnapshot = await snapshotWorkspace(sb)
    }

    const toolExecution = (openaiOptions.toolExecution ?? {}) as {
      maxFunctionToolConcurrency?: number | null
    }
    openaiOptions.toolExecution = {
      ...toolExecution,
      maxFunctionToolConcurrency:
        toolExecution.maxFunctionToolConcurrency ?? 1,
    }

    if (streaming) {
      bindStreamSandbox(sb)
      try {
        const result = await openaiRun(
          agent,
          input,
          openaiOptions as Parameters<typeof openaiRun>[2],
        )
        attachStreamSandboxLifecycle(sb, result as { completed: Promise<void> }, ws, inputSnapshot)
        return result
      } catch (err) {
        unbindStreamSandbox(sb)
        await sb.stop()
        throw err
      }
    }

    const result = await sandboxStore.run(sb, () =>
      openaiRun(agent, input, openaiOptions as Parameters<typeof openaiRun>[2]),
    )

    await emitFileOutputs(sb, ws, inputSnapshot)
    return result
  } finally {
    if (!streaming) {
      await sb.stop()
    }
  }
}

async function createSandbox(name: string, config: NonNullable<ReturnType<typeof discoverSandboxConfig>>) {
  let builder = Sandbox.builder(name)
    .image(config.image)
    .cpus(config.cpus ?? 1)
    .memory(config.memory ?? 256)
    .ephemeral(!config.persist)
    .replace()

  const pypiDomains = config.packages?.length
    ? ['pypi.org', 'files.pythonhosted.org']
    : []
  const secretHosts = (config.secrets ?? []).map((s) => s.host)
  const explicitDomains =
    config.network !== false &&
    config.network !== true &&
    config.network != null
      ? (config.network.allow ?? [])
      : []
  const allDomains = [
    ...new Set([...explicitDomains, ...secretHosts, ...pypiDomains]),
  ]

  if (config.network === true) {
    // full unrestricted — no policy applied
  } else if (allDomains.length > 0) {
    builder = builder.network((n) =>
      n.policy((p: any) => p.defaultDeny().egress((r: any) => r.allowDomains(allDomains))),
    )
  } else {
    builder = builder.disableNetwork()
  }

  for (const secret of config.secrets ?? []) {
    builder = builder.secretEnv(secret.env, secret.value, secret.host)
  }

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

  return builder.create()
}
