import type { SandboxBuilder } from 'microsandbox'

export type SandboxVolumeMount = {
  name: string
  mountPath: string
  createIfMissing?: boolean
  readonly?: boolean
}

export type SandboxRunOptions = {
  image: string
  interpreter: string
  cpus?: number
  memory?: number
  network?: boolean
  timeoutSecs?: number
  env?: Record<string, string>
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
