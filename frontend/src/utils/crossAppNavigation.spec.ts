import { describe, expect, it } from 'vitest'

import { buildStreamlitUrl, parseArtifactReference } from './crossAppNavigation'

describe('cross-app navigation', () => {
  it('returns to the exact originating Streamlit thread', () => {
    expect(buildStreamlitUrl('thread with space')).toBe(
      'http://localhost:8501/?thread_id=thread+with+space',
    )
  })

  it('accepts an artifact id or a complete result URL', () => {
    expect(parseArtifactReference('A'.repeat(32))).toEqual({ artifactId: 'a'.repeat(32) })
    expect(
      parseArtifactReference(`/visualizations/${'b'.repeat(32)}/3d?thread_id=thread-2`),
    ).toEqual({ artifactId: 'b'.repeat(32), threadId: 'thread-2' })
  })

  it('rejects paths that are not a bounded artifact route', () => {
    expect(parseArtifactReference('../../secret')).toBeNull()
    expect(parseArtifactReference('/visualizations/not-an-id/other')).toBeNull()
    expect(parseArtifactReference('ab')).toBeNull()
  })

  it('accepts semantic artifact ids from the 3d route', () => {
    const id =
      'regression-scat-gbar-v23-scat-scat20161015_20161021-100015-h360-n36-20260917T103045-a3f2c1'
    expect(parseArtifactReference(`/visualizations/${id}/3d`)?.artifactId).toBe(id)
    expect(parseArtifactReference(id)?.artifactId).toBe(id)
  })
})
