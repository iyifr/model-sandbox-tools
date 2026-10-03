import fs from 'node:fs/promises'
import { Sandbox, SandboxNotFoundError } from 'microsandbox'
import { assertSafeFilename, SandboxSetupError } from './core/index.js'
import { resolveWorkspaceRoot, sessionDir } from './host-workspace.js'

/** Thrown when a follow-up turn (`skipInputSeed: true`) targets a session that no longer exists. */
export class SessionNotFoundError extends SandboxSetupError {
  readonly sessionName: string

  constructor(sessionName: string, reason: string) {
    super(`[mst] Session ${JSON.stringify(sessionName)} not found: ${reason}. Re-seed it with skipInputSeed: false.`)
    this.name = 'SessionNotFoundError'
    this.sessionName = sessionName
  }
}

export type SessionInfo = {
  name: string
  /** Host directory mounted at /workspace. */
  workspaceDir: string
  /** State of the session's VM; 'missing' if it was removed (e.g. endSession with keepFiles). */
  sandbox: 'running' | 'stopped' | 'missing'
  /** End of the most recent run() on this session. */
  lastUsed: Date
}

export type SessionOptions = {
  /** Must match the workspaceRoot passed to WorkspaceContext for this session. */
  workspaceRoot?: string
}

/** Session names become directory names, and dot-names are reserved for MST. */
export function assertSessionName(name: string) {
  assertSafeFilename(name)
  if (name.startsWith('.')) {
    throw new Error(`[mst] Invalid session name: ${JSON.stringify(name)}`)
  }
}

const sessionQueues = new Map<string, Promise<void>>()

/** Runs sharing a sandboxName take turns, so one can't replace the other's VM mid-run. */
export async function acquireSession(name: string): Promise<() => void> {
  const previous = sessionQueues.get(name) ?? Promise.resolve()
  let release!: () => void
  const current = new Promise<void>((resolve) => {
    release = resolve
  })
  const queued = previous.then(() => current)
  sessionQueues.set(name, queued)
  await previous
  return () => {
    release()
    if (sessionQueues.get(name) === queued) sessionQueues.delete(name)
  }
}

async function sandboxState(name: string): Promise<SessionInfo['sandbox']> {
  try {
    const handle = await Sandbox.get(name)
    return handle.status === 'running' ? 'running' : 'stopped'
  } catch (err) {
    if (err instanceof SandboxNotFoundError) return 'missing'
    throw err
  }
}

/** Persistent sessions under `workspaceRoot`, most recently used first. */
export async function listSessions(options: SessionOptions = {}): Promise<SessionInfo[]> {
  const root = resolveWorkspaceRoot(options.workspaceRoot)
  const entries = await fs.readdir(root, { withFileTypes: true }).catch((err) => {
    if (err.code === 'ENOENT') return []
    throw err
  })

  const sessions: SessionInfo[] = []
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue
    const workspaceDir = sessionDir(entry.name, options.workspaceRoot)
    const { mtime } = await fs.stat(workspaceDir)
    sessions.push({
      name: entry.name,
      workspaceDir,
      sandbox: await sandboxState(entry.name),
      lastUsed: mtime,
    })
  }
  return sessions.sort((a, b) => b.lastUsed.getTime() - a.lastUsed.getTime())
}

/**
 * Delete a persistent session's VM and, unless `keepFiles`, its workspace files.
 * Waits for queued run() calls on the session to finish first.
 */
export async function endSession(
  name: string,
  options: SessionOptions & { keepFiles?: boolean } = {},
): Promise<void> {
  assertSessionName(name)
  const release = await acquireSession(name)
  try {
    await Sandbox.remove(name).catch((err) => {
      if (!(err instanceof SandboxNotFoundError)) throw err
    })
    if (!options.keepFiles) {
      await fs.rm(sessionDir(name, options.workspaceRoot), { recursive: true, force: true })
    }
  } finally {
    release()
  }
}
