import type { Sandbox } from 'microsandbox'
import { formatYaml } from '../core/index.js'

export const DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024

export type ExecLimits = {
  timeoutMs: number
  maxOutputBytes: number
  signal?: AbortSignal
}

/**
 * Keeps the first and last `maxBytes / 2` of a stream and counts what falls in
 * between, so memory stays bounded however much the command prints.
 */
class CappedOutput {
  private head: Buffer[] = []
  private headBytes = 0
  private tail: Buffer[] = []
  private tailBytes = 0
  private total = 0
  private readonly half: number

  constructor(maxBytes: number) {
    this.half = Math.floor(maxBytes / 2)
  }

  push(data: Uint8Array) {
    let chunk = Buffer.from(data)
    this.total += chunk.length
    if (this.headBytes < this.half) {
      const take = Math.min(this.half - this.headBytes, chunk.length)
      this.head.push(chunk.subarray(0, take))
      this.headBytes += take
      chunk = chunk.subarray(take)
    }
    if (chunk.length === 0) return
    this.tail.push(chunk)
    this.tailBytes += chunk.length
    while (this.tail.length > 1 && this.tailBytes - this.tail[0]!.length >= this.half) {
      this.tailBytes -= this.tail.shift()!.length
    }
  }

  toString(): string {
    let tail = Buffer.concat(this.tail)
    if (tail.length > this.half) tail = tail.subarray(tail.length - this.half)
    const omitted = this.total - this.headBytes - tail.length
    const head = Buffer.concat(this.head).toString('utf8')
    if (omitted <= 0) return head + tail.toString('utf8')
    return (
      `${head}\n... [mst] ${omitted} bytes omitted. ` +
      `Redirect large output to a file and inspect it with grep/head/tail ...\n` +
      tail.toString('utf8')
    )
  }
}

/**
 * Run a command and return the YAML tool result. Kills the process on timeout
 * (exit 124) or when the run is aborted. Streamed exec ignores the builder's
 * own timeout, so the timer lives here.
 */
export async function execToYaml(
  sb: Sandbox,
  cmd: string,
  args: string[],
  { timeoutMs, maxOutputBytes, signal }: ExecLimits,
): Promise<string> {
  signal?.throwIfAborted()
  const handle = await sb.execStreamWith(cmd, (e) => e.args(args))

  let timedOut = false
  const kill = () => handle.kill().catch(() => {})
  const timer = setTimeout(() => {
    timedOut = true
    kill()
  }, timeoutMs)
  signal?.addEventListener('abort', kill, { once: true })

  const stdout = new CappedOutput(maxOutputBytes)
  const stderr = new CappedOutput(maxOutputBytes)
  let code = -1
  try {
    for await (const event of handle) {
      if (event.kind === 'stdout') stdout.push(event.data)
      else if (event.kind === 'stderr') stderr.push(event.data)
      else if (event.kind === 'exited') code = event.code
    }
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', kill)
  }

  if (timedOut) {
    return formatYaml({
      exit_code: 124,
      stdout: stdout.toString(),
      stderr: `${stderr.toString()}exec timed out after ${timeoutMs}ms`,
    })
  }
  return formatYaml({ exit_code: code, stdout: stdout.toString(), stderr: stderr.toString() })
}
