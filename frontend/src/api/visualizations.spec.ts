import { beforeEach, expect, it, vi } from 'vitest'
import { memoryRequest } from './memory'
import { chartMemoryToken } from './visualizations'

vi.mock('./memory', () => ({ memoryRequest: vi.fn() }))
beforeEach(() => vi.resetAllMocks())

it('reads memory state for every chart request without a keyword gate', async () => {
  vi.mocked(memoryRequest).mockResolvedValue({ enabled: true, epoch: 1, revision: 3, items: [] })
  expect(await chartMemoryToken()).toMatchObject({ epoch: 1, revision: 3 })
  expect(memoryRequest).toHaveBeenCalledWith('/chart-preferences')
})

it('leaves a failed memory read unavailable instead of inventing a version', async () => {
  vi.mocked(memoryRequest).mockRejectedValue(Error('unavailable'))
  expect(await chartMemoryToken()).toBeUndefined()
})
