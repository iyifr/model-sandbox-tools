import { tool } from '@openai/agents'
import { z } from 'zod'
import { getActiveSandbox } from 'mst-core'

export function sandboxListFiles() {
  return tool({
    name: 'sandbox_list_files',
    description: 'List files in a directory inside the sandbox.',
    parameters: z.object({
      path: z.string().default('/workspace').describe('Directory path to list'),
    }),
    execute: async ({ path }) => {
      const entries = await getActiveSandbox().fs().list(path)
      return entries
        .map((e) => `${e.kind === 'directory' ? 'd' : 'f'}  ${e.path}`)
        .join('\n')
    },
  })
}
