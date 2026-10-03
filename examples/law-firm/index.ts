import fs from 'node:fs'
import path from 'node:path'

import { run } from 'openai-agents-msb'
import { WorkspaceContext } from 'openai-microsandbox-core'

import { createLawyerAgent } from './lawyer-agent.js'

const ROOT = import.meta.dir
const agent = createLawyerAgent()

const templateBytes = fs.readFileSync(path.join(ROOT, 'template.docx'))
const caseNotesBytes = fs.readFileSync(path.join(ROOT, 'case_notes.md'))

const result = await run(
  agent,
  'Use the case notes to fill in the legal brief template. Save the completed brief as completed_brief.docx',
  WorkspaceContext({
    inputFiles: [
      { name: 'template.docx', data: templateBytes },
      { name: 'case_notes.md', data: caseNotesBytes },
    ],
    onFileOutput: async (payload) => {
      if (payload.file_name === 'completed_brief.docx') {
        fs.mkdirSync(path.join(ROOT, 'output'), { recursive: true })
        fs.copyFileSync(payload.path, path.join(ROOT, 'output', 'completed_brief.docx'))
        console.log(`Saved ${payload.file_name} (version ${payload.version})`)
      }
    },
  }),
)

console.log(result.finalOutput)
