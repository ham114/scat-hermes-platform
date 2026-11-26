import { describe, expect, it } from 'vitest'
import { summarizeTokenUsage, tokenUsage, type DiagnosticNode } from './diagnostics'

const generation = (usage: Record<string, number>) =>
  ({ kind: 'generation', usage }) as DiagnosticNode
describe('diagnostic token accounting', () => {
  it('includes cache input once and avoids counting reasoning twice', () => {
    expect(
      tokenUsage(
        generation({
          input: 10,
          output: 20,
          cache_read_input_tokens: 30,
          cache_creation_input_tokens: 5,
          reasoning_tokens: 12,
          total: 999,
        }),
      ),
    ).toMatchObject({
      input: 45,
      output: 20,
      cached: 30,
      written: 5,
      reasoning: 12,
      total: 65,
    })
  })
  it('distinguishes no generation, missing usage and incomplete totals', () => {
    expect(summarizeTokenUsage([]).state).toBe('no_generation')
    expect(summarizeTokenUsage([generation({})]).state).toBe('missing')
    expect(
      summarizeTokenUsage([generation({ input: 10, output: 2 }), generation({})]),
    ).toMatchObject({ state: 'partial', total: 12, known: 1, generations: 2 })
  })
  it('keeps explicit zero usage and rejects malformed provider values', () => {
    expect(summarizeTokenUsage([generation({ input: 0, output: 0 })])).toMatchObject({
      state: 'complete',
      total: 0,
    })
    expect(summarizeTokenUsage([generation({ input: -1, output: Number.NaN })]).state).toBe(
      'missing',
    )
  })
})
