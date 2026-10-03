import { tool } from '@openai/agents'
import { MST_TOOL, SandboxSetupError } from 'openai-microsandbox-core'

/**
 * Like tool(), but sandbox setup failures fail the run instead of being shown to
 * the model, and the tool is marked so run() can find MST tools on an agent.
 */
export const mstTool = ((options: Parameters<typeof tool>[0]) => {
  const t = tool({
    ...options,
    errorFunction: (_context: unknown, error: unknown) => {
      if (error instanceof SandboxSetupError) throw error
      // Same text as the SDK's default error function.
      const details = error instanceof Error ? error.toString() : String(error)
      return `An error occurred while running the tool. Please try again. Error: ${details}`
    },
  } as Parameters<typeof tool>[0])
  Object.assign(t, { [MST_TOOL]: true })
  return t
}) as typeof tool
