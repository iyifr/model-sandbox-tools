export function assertSafeFilename(name: string): void {
  if (!name || name.includes('/') || name.includes('\\') || name.includes('..')) {
    throw new Error(`[mst] Invalid filename: ${JSON.stringify(name)}`)
  }
}
