import fs from 'node:fs/promises'
import { run as openaiRun } from '@openai/agents'
import type {
  Agent,
  AgentInputItem,
  NonStreamRunOptions,
  RunResult,
  RunState,
  StreamedRunResult,
  StreamRunOptions,
} from '@openai/agents'

import { Sandbox, SandboxNotFoundError, Volume } from 'microsandbox'
import type { Sandbox as SandboxInstance } from 'microsandbox'
import { sandboxStore, SandboxSetupError, WORKSPACE_CTX } from 'openai-microsandbox-core'
import type { SandboxProvider, SandboxRunOptions, WorkspaceContextOptions } from 'openai-microsandbox-core'
import { DEFAULT_SANDBOX_CONFIG, configKey, discoverSandboxConfig } from './discover-config.js'
import { applyNetwork, validateNetwork } from './network.js'
import {
  GUEST_WORKSPACE,
  assertInputNames,
  sessionDir,
  clearWorkspaceDir,
  createWorkspaceDir,
  emitFileOutputs,
  seedInputFiles,
  snapshotWorkspace,
} from './host-workspace.js'
import type { WorkspaceSnapshot } from './host-workspace.js'
import { SessionNotFoundError, acquireSession, assertSessionName } from './session.js'

type WorkspaceOption = { [WORKSPACE_CTX]?: WorkspaceContextOptions }

/** `@openai/agents` run options plus the workspace set by `WorkspaceContext()`. */
export type MstNonStreamRunOptions<
  TContext = undefined,
  TAgent extends Agent<any, any> = Agent<any, any>,
> = NonStreamRunOptions<TContext, TAgent> & WorkspaceOption

export type MstStreamRunOptions<
  TContext = undefined,
  TAgent extends Agent<any, any> = Agent<any, any>,
> = StreamRunOptions<TContext, TAgent> & WorkspaceOption

type RunInput<TContext, TAgent extends Agent<any, any>> =
  | string
  | AgentInputItem[]
  | RunState<TContext, TAgent>

function attachStreamSandboxLifecycle(
  result: { completed: Promise<void> },
  finish: () => Promise<void>,
) {
  const completed = result.completed.finally(finish)
  // Still rejects for callers awaiting it; just never reported as unhandled.
  completed.catch(() => {})

  // `completed` is a getter on StreamedRunResult; shadow it so awaiting it includes teardown.
  Object.defineProperty(result, 'completed', { value: completed })
}

/** Drop-in for `run()` from `@openai/agents` that gives the agent's sandbox tools a microsandbox VM. */
export function run<TAgent extends Agent<any, any>, TContext = undefined>(
  agent: TAgent,
  input: RunInput<TContext, TAgent>,
  options?: MstNonStreamRunOptions<TContext, TAgent>,
): Promise<RunResult<TContext, TAgent>>
export function run<TAgent extends Agent<any, any>, TContext = undefined>(
  agent: TAgent,
  input: RunInput<TContext, TAgent>,
  options?: MstStreamRunOptions<TContext, TAgent>,
): Promise<StreamedRunResult<TContext, TAgent>>
export async function run(
  agent: Agent<any, any>,
  input: RunInput<any, Agent<any, any>>,
  options?: MstNonStreamRunOptions<any> | MstStreamRunOptions<any>,
): Promise<RunResult<any, Agent<any, any>> | StreamedRunResult<any, Agent<any, any>>> {
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
  const ws = workspace as WorkspaceContextOptions | undefined

  const discovered = discoverSandboxConfig(agent)
  if (discovered.config) validateNetwork(discovered.config)

  // Session settings come from the reachable sandboxRun(); asTool() agents can't be inspected.
  const persistentSession = discovered.config?.persist === true && ws?.sandboxName != null
  const reusingPersistedSandbox = persistentSession && ws.skipInputSeed === true
  const name = ws?.sandboxName ?? `mst-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`
  if (persistentSession) assertSessionName(name)
  // Checked now: the workspace is only seeded once a sandbox tool runs.
  if (ws?.inputFiles?.length && !ws.skipInputSeed) assertInputNames(ws.inputFiles)

  const releaseSession = ws?.sandboxName ? await acquireSession(ws.sandboxName) : () => {}
  let sandbox: Promise<SandboxInstance> | undefined
  let sandboxKey: string | undefined
  let sb: SandboxInstance | undefined
  let hostDir: string | undefined
  let inputSnapshot: WorkspaceSnapshot = new Map()
  let handedOff = false

  // Stop the VM before reading outputs so nothing in the guest can change /workspace mid-read.
  const finish = async (emit: boolean) => {
    try {
      await sandbox?.catch(() => {})
      await sb?.stop()
    } finally {
      // An ephemeral VM that died mid-run is not removed by stop().
      if (sb && !persistentSession) await Sandbox.remove(name).catch(() => {})
      try {
        if (emit && hostDir) await emitFileOutputs(hostDir, ws, inputSnapshot)
      } finally {
        if (hostDir && !persistentSession) await fs.rm(hostDir, { recursive: true, force: true })
        // listSessions() reports this as lastUsed.
        if (hostDir && persistentSession && sb) {
          const now = new Date()
          await fs.utimes(hostDir, now, now).catch(() => {})
        }
        releaseSession()
      }
    }
  }

  const startSandbox = async (config: SandboxRunOptions): Promise<SandboxInstance> => {
    if (reusingPersistedSandbox) {
      sb = await Sandbox.start(name).catch((err) => {
        if (err instanceof SandboxNotFoundError) {
          throw new SessionNotFoundError(name, 'sandbox was removed')
        }
        throw err
      })
      return sb
    }

    hostDir = await createWorkspaceDir(name, persistentSession, ws?.workspaceRoot)
    await clearWorkspaceDir(hostDir)
    if (ws?.inputFiles?.length && !ws.skipInputSeed) {
      inputSnapshot = await seedInputFiles(hostDir, ws.inputFiles)
    }
    sb = await createSandbox(name, config, hostDir, persistentSession)

    if (config.packages?.length) {
      const out = await sb.shell(`pip install --quiet ${config.packages.join(' ')}`)
      if (!out.success) {
        throw new Error(`[mst] pip install failed (exit ${out.code}):\n${out.stderr()}`)
      }
    }
    return sb
  }

  // Created on the first sandbox tool call, so handoffs, asTool() agents and
  // tools without a sandboxRun() all share one VM per run.
  const provider: SandboxProvider = {
    get(requested) {
      const config = requested ?? discovered.config ?? DEFAULT_SANDBOX_CONFIG
      if (sandbox) {
        if (requested && configKey(requested) !== sandboxKey) {
          return Promise.reject(new SandboxSetupError(
            '[mst] All sandboxRun() tools used in one run must use identical SandboxRunOptions',
          ))
        }
        return sandbox
      }
      sandboxKey = configKey(config)
      sandbox = (async () => {
        try {
          validateNetwork(config)
          return await startSandbox(config)
        } catch (err) {
          if (err instanceof SessionNotFoundError) throw err
          throw new SandboxSetupError(err instanceof Error ? err.message : String(err), { cause: err })
        }
      })()
      return sandbox
    },
  }

  try {
    // Fail before the model runs if a follow-up turn targets a deleted session.
    if (reusingPersistedSandbox) {
      hostDir = await fs.realpath(sessionDir(name, ws.workspaceRoot)).catch(() => {
        throw new SessionNotFoundError(name, 'workspace directory is missing')
      })
      await Sandbox.get(name).catch((err) => {
        if (err instanceof SandboxNotFoundError) {
          throw new SessionNotFoundError(name, 'sandbox was removed')
        }
        throw err
      })
      inputSnapshot = await snapshotWorkspace(hostDir)
    }

    if (discovered.hasTools) {
      const toolExecution = (openaiOptions.toolExecution ?? {}) as {
        maxFunctionToolConcurrency?: number | null
      }
      openaiOptions.toolExecution = {
        ...toolExecution,
        maxFunctionToolConcurrency:
          toolExecution.maxFunctionToolConcurrency ?? 1,
      }
    }

    const result = await sandboxStore.run(provider, () =>
      openaiRun(agent, input, openaiOptions as Parameters<typeof openaiRun>[2]),
    )
    handedOff = true

    if (streaming) {
      attachStreamSandboxLifecycle(result as { completed: Promise<void> }, () => finish(true))
      return result
    }
    await finish(true)
    return result
  } finally {
    // Setup or a non-streamed run failed: clean up without masking the original error.
    if (!handedOff) await finish(false).catch(() => {})
  }
}

async function createSandbox(
  name: string,
  config: SandboxRunOptions,
  hostWorkspaceDir: string,
  persistent: boolean,
) {
  let builder = Sandbox.builder(name)
    .image(config.image)
    .cpus(config.cpus ?? 1)
    .memory(config.memory ?? 256)
    .ephemeral(!persistent)
    .replace()
    .volume(GUEST_WORKSPACE, (m) => m.bind(hostWorkspaceDir))

  builder = applyNetwork(builder, config)

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
