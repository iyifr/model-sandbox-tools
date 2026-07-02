import { WORKSPACE_CTX } from './symbols.js'
import type { WorkspaceContextOptions, WorkspaceInput } from './types.js'

export function WorkspaceContext(opts: WorkspaceContextOptions) {
  return { [WORKSPACE_CTX]: opts }
}

export function normalizeFile(
  f: Exclude<WorkspaceInput, File>,
): { name: string; data: Uint8Array } {
  return {
    name: f.name,
    data: f.data instanceof Buffer ? new Uint8Array(f.data) : f.data,
  }
}
