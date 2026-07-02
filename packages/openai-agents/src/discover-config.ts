import type { Agent } from '@openai/agents'
import { MST_SANDBOX_CONFIG } from 'mst-core'
import type { SandboxRunOptions } from 'mst-core'

function configKey(c: SandboxRunOptions): string {
  return JSON.stringify({
    image: c.image,
    interpreter: c.interpreter,
    cpus: c.cpus ?? 1,
    memory: c.memory ?? 256,
    network: c.network ?? false,
    packages: [...(c.packages ?? [])].sort(),
  })
}

export function discoverSandboxConfig(
  agent: Agent<any, any>,
): SandboxRunOptions | undefined {
  const configs = agent.tools
    .map((t) => (t as { [MST_SANDBOX_CONFIG]?: SandboxRunOptions })[MST_SANDBOX_CONFIG])
    .filter(Boolean) as SandboxRunOptions[]

  if (configs.length === 0) return undefined
  if (configs.length === 1) return configs[0]

  const first = configKey(configs[0])
  if (configs.some((c) => configKey(c) !== first)) {
    throw new Error(
      '[mst] All sandboxRun() tools on an agent must use identical SandboxRunOptions',
    )
  }
  return configs[0]
}
