import { tool } from '@openai/agents'
import { z } from 'zod'
import {
  getActiveSandbox,
  formatYaml,
  MST_SANDBOX_CONFIG,
} from 'mst-core'
import type { SandboxRunOptions } from 'mst-core'
import { ExecTimeoutError } from 'microsandbox'

export function sandboxRun(options: SandboxRunOptions) {
  const timeoutMs = (options.timeoutSecs ?? 30) * 1000

  const t = tool({
    name: 'sandbox_run',
    description:
      'Execute a script inside an isolated sandbox. Returns exit code, stdout, and stderr as YAML.',
    parameters: z.object({
      script: z.string().describe('The script content to execute'),
    }),
    execute: async ({ script }) => {
      const sb = getActiveSandbox()
      const scriptPath = `/tmp/mst_script_${crypto.randomUUID()}`
      try {
        await sb.fs().write(scriptPath, script)
        const out = await sb.execWith(options.interpreter, (e) =>
          e.args([scriptPath]).timeout(timeoutMs),
        )
        return formatYaml({
          exit_code: out.code,
          stdout: out.stdout(),
          stderr: out.stderr(),
        })
      } catch (err) {
        if (err instanceof ExecTimeoutError) {
          return formatYaml({
            exit_code: 124,
            stdout: '',
            stderr: `exec timed out after ${err.timeoutMs}ms`,
          })
        }
        throw err
      } finally {
        await sb.fs().remove(scriptPath).catch(() => {})
      }
    },
  })

  Object.assign(t, { [MST_SANDBOX_CONFIG]: options })
  return t
}
