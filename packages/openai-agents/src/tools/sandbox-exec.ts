import { z } from 'zod'
import { mstTool } from './mst-tool.js'
import { getActiveSandbox } from '@iyifr/mst-core'
import { DEFAULT_MAX_OUTPUT_BYTES, execToYaml } from './exec.js'

export type SandboxExecOptions = {
  /** Kill the command after this many seconds. Default: 600. */
  timeoutSecs?: number
  /** Max bytes of stdout and of stderr returned to the model; the middle is dropped. Default: 32768. */
  maxOutputBytes?: number
}

export function sandboxExec(options: SandboxExecOptions = {}) {
  const timeoutMs = (options.timeoutSecs ?? 600) * 1000
  const maxOutputBytes = options.maxOutputBytes ?? DEFAULT_MAX_OUTPUT_BYTES

  return mstTool({
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
    execute: async ({ command }, _context, details) =>
      execToYaml(await getActiveSandbox(), '/bin/sh', ['-c', command], {
        timeoutMs,
        maxOutputBytes,
        signal: details?.signal,
      }),
  })
}
