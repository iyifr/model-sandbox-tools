import { AsyncLocalStorage } from "node:async_hooks";
import type { Sandbox } from "microsandbox";

export const sandboxStore = new AsyncLocalStorage<Sandbox>();

export function getActiveSandbox(): Sandbox {
  const sb = sandboxStore.getStore();
  if (!sb) {
    throw new Error(
      "[mst] No active sandbox. Wrap your run() call with @mst/openai-agents run()",
    );
  }
  return sb;
}
