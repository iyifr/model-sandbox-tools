import { tool } from '@openai/agents'
import { z } from 'zod'
import { getActiveSandbox, formatYaml } from 'mst-core'

export function sandboxExec() {
  return tool({
    name: 'sandbox_exec',
    description:
      'Run a shell command in the sandbox and return exit code, stdout, and stderr as YAML. ' +
      'Use for invoking installed binaries and CLI tools (e.g. pytest, black, node, ffmpeg). ' +
      'For writing and running a custom script use sandbox_run instead.',
    parameters: z.object({
      command: z.string().describe(
        'Shell command to run (e.g. "python3 -m pytest /workspace/tests/ -v")',
      ),
    }),
    execute: async ({ command }) => {
      const out = await getActiveSandbox().shell(command)
      return formatYaml({
        exit_code: out.code,
        stdout: out.stdout(),
        stderr: out.stderr(),
      })
    },
  })
}
