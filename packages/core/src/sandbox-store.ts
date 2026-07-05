import { AsyncLocalStorage } from 'node:async_hooks'
import type { Sandbox } from 'microsandbox'

export const sandboxStore = new AsyncLocalStorage<Sandbox>()

/** Fallback for streamed runs where tool calls happen outside ALS scope. */
let streamSandbox: Sandbox | undefined

export function bindStreamSandbox(sb: Sandbox) {
  streamSandbox = sb
}

export function unbindStreamSandbox(sb: Sandbox) {
  if (streamSandbox === sb) streamSandbox = undefined
}

export function getActiveSandbox(): Sandbox {
  const sb = sandboxStore.getStore() ?? streamSandbox
  if (!sb) {
    throw new Error(
      '[mst] No active sandbox. Wrap your run() call with @mst/openai-agents run()',
    )
  }
  return sb
}
