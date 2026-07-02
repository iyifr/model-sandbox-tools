import type { SandboxBuilder } from 'microsandbox'

export type SandboxVolumeMount = {
  name: string
  mountPath: string
  createIfMissing?: boolean
  readonly?: boolean
}

export type NetworkConfig =
  | false                   // explicit lockdown — throws if packages or secrets present
  | true                    // full unrestricted — escape hatch
  | { allow?: string[] }    // allowlist — merged with hosts from secrets + pypi from packages

export type SandboxSecret = {
  env: string     // env var name inside the sandbox
  value: string   // the secret value (from vault, process.env, etc.)
  host: string    // only exposed to connections to this host; auto-added to network allowlist
}

export type SandboxRunOptions = {
  image: string
  interpreter: string
  cpus?: number
  memory?: number
  network?: NetworkConfig
  timeoutSecs?: number
  env?: Record<string, string>
  secrets?: SandboxSecret[]
  packages?: string[]
  volumes?: SandboxVolumeMount[]
  configure?: (b: SandboxBuilder) => SandboxBuilder
}

export type FileOutPayload = {
  file_name: string
  version: number
  buffer: Uint8Array
}

export type WorkspaceInput =
  | File
  | { name: string; data: Buffer | Uint8Array }

export type WorkspaceContextOptions = {
  inputFiles?: WorkspaceInput[]
  onFileOutput?: (payload: FileOutPayload) => void | Promise<void>
}
