import { Agent } from '@openai/agents'
import { run, sandboxRun, sandboxListFiles, sandboxReadFile } from '@mst/openai-agents'
import { WorkspaceContext } from 'mst-core'
import fs from 'node:fs'

const agent = new Agent({
  name: 'legal-document-agent',
  instructions: `
    You are a legal document assistant working inside an isolated sandbox.
    Files are available at /workspace/.
    Use sandbox_list_files to discover them.
    Use sandbox_read_file for text files.
    Use sandbox_run with Python for binary formats (docx, pdf).
    Before processing docx files, run: pip install python-docx pypdf
    When finished editing, save the result back to /workspace/.
  `,
  tools: [
    sandboxRun({
      image: 'python:3.12-slim',
      interpreter: 'python3',
      network: true,
      timeoutSecs: 120,
    }),
    sandboxListFiles(),
    sandboxReadFile(),
  ],
})

const templateBytes = fs.readFileSync('./template.docx')
const caseNotesBytes = fs.readFileSync('./case_notes.md')

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
        fs.writeFileSync('./output/completed_brief.docx', payload.buffer)
        console.log(`Saved ${payload.file_name} (version ${payload.version})`)
      }
    },
  }),
)

console.log(result.finalOutput)
