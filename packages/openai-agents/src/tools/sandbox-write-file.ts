import { tool } from '@openai/agents'
import { z } from 'zod'
import { getActiveSandbox } from 'mst-core'

export function sandboxWriteFile() {
  return tool({
    name: 'sandbox_write_file',
    description:
      'Write text content directly to a file in the sandbox. ' +
      'Use for writing reports, configs, JSON, markdown, or any finished text artifact. ' +
      'For binary file manipulation use sandbox_run with Python instead.',
    parameters: z.object({
      path: z.string().describe('Absolute path to write to (e.g. /workspace/report.md)'),
      content: z.string().describe('Text content to write'),
    }),
    execute: async ({ path, content }) => {
      if (!path.startsWith('/') || path.includes('..')) {
        return `[mst] invalid path: ${JSON.stringify(path)}`
      }

      const sb = getActiveSandbox()

      // Ensure every ancestor directory exists before writing
      const segments = path.split('/').filter(Boolean)
      segments.pop()
      let current = ''
      for (const segment of segments) {
        current += `/${segment}`
        await sb.fs().mkdir(current).catch(() => {})
      }

      await sb.fs().write(path, content)
      return `written ${path} (${Buffer.byteLength(content, 'utf8')} bytes)`
    },
  })
}
