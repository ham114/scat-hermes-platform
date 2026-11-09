const DEFAULT_STREAMLIT_URL = 'http://localhost:8501'

export const streamlitBaseUrl = import.meta.env.VITE_STREAMLIT_URL || DEFAULT_STREAMLIT_URL

export function buildStreamlitUrl(threadId?: string) {
  const origin = globalThis.location?.origin ?? 'http://localhost'
  const url = new URL(streamlitBaseUrl, origin)
  if (threadId) url.searchParams.set('thread_id', threadId)
  return url.toString()
}

export interface ArtifactReference {
  artifactId: string
  threadId?: string
}

export function parseArtifactReference(value: string): ArtifactReference | null {
  const candidate = value.trim()
  const LEGACY_HEX = /^[0-9a-f]{32}$/i
  const SEMANTIC = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$/
  if (LEGACY_HEX.test(candidate)) {
    return { artifactId: candidate.toLowerCase() }
  }
  if (SEMANTIC.test(candidate)) {
    return { artifactId: candidate }
  }

  try {
    const origin = globalThis.location?.origin ?? 'http://localhost'
    const url = new URL(candidate, origin)
    const match = url.pathname.match(/^\/visualizations\/([^/]+)\/3d\/?$/i)
    if (!match) return null
    const artifactId = decodeURIComponent(match[1])
    if (!LEGACY_HEX.test(artifactId) && !SEMANTIC.test(artifactId)) return null
    const threadId = url.searchParams.get('thread_id')?.trim()
    return {
      artifactId: LEGACY_HEX.test(artifactId) ? artifactId.toLowerCase() : artifactId,
      ...(threadId && threadId.length <= 200 ? { threadId } : {}),
    }
  } catch {
    return null
  }
}
