import { tool } from '@openai/agents'
import { z } from 'zod'
import { getActiveSandbox } from 'mst-core'

const MAX_READ_BYTES = 1024 * 1024

export function sandboxReadFile() {
  return tool({
    name: 'sandbox_read_file',
    description:
      'Read a text file from the sandbox. For binary files (docx, pdf) use sandbox_run with Python instead.',
    parameters: z.object({
      path: z.string().describe('Absolute path to the file in the sandbox'),
    }),
    execute: async ({ path }) => {
      const fs = getActiveSandbox().fs()
      const meta = await fs.stat(path)
      if (meta.size > MAX_READ_BYTES) {
        return `[mst] File too large (${meta.size} bytes). Use sandbox_run with Python for files over 1 MiB.`
      }
      return fs.readToString(path)
    },
  })
}
