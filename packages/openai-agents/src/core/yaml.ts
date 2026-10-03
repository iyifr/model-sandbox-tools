export function formatYaml(result: {
  exit_code: number
  stdout: string
  stderr: string
}): string {
  const block = (s: string) =>
    s ? `|\n${s.split('\n').map((l) => `  ${l}`).join('\n')}` : '""'
  return [
    `exit_code: ${result.exit_code}`,
    `stdout: ${block(result.stdout)}`,
    `stderr: ${block(result.stderr)}`,
  ].join('\n')
}
