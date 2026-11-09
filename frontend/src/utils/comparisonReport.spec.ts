import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AnalysisArtifactView } from '@/api/client'
import type { Track3D } from '@/visualization/types'

import {
  buildComparisonSnapshotReportHtml,
  downloadComparisonSnapshotReport,
} from './comparisonReport'

const artifact = {
  schema_version: '1.0',
  artifact_id: 'comparison-artifact-1234',
  run_id: 'a'.repeat(32),
  kind: 'analysis_result',
  thread_id: 'private-thread-must-not-leak',
  model_key: 'scat-comparison-v1',
  model_version: '1',
  analysis_result: {
    schema_version: '1.6',
    dataset: 'scat',
    operation: 'run_model_comparison',
    title: '统一预测对比结果',
    context: {
      protocol_version: 'gbar-align-v1',
      sample_set_sha256: 'b'.repeat(64),
    },
    blocks: [{ type: 'metrics', title: '摘要', items: [] }],
  },
  created_at: '2026-09-22T00:00:00Z',
  expires_at: null,
} satisfies AnalysisArtifactView

const tracks: Track3D[] = [
  {
    id: 'snapshot::actual',
    snapshotId: 'snapshot',
    label: '实际轨迹',
    kind: 'actual_future',
    color: '#16a34a',
    points: [
      {
        time: 10,
        longitude: 20,
        latitude: 60,
        altitudeMetres: 3_000,
        groundSpeedMps: 100,
        horizontalErrorMetres: null,
        segment: 'actual_future',
      },
      {
        time: 20,
        longitude: 20.1,
        latitude: 60.1,
        altitudeMetres: 3_100,
        groundSpeedMps: 100,
        horizontalErrorMetres: null,
        segment: 'actual_future',
      },
    ],
  },
  {
    id: 'snapshot::kalman-cv',
    snapshotId: 'snapshot',
    label: 'Kalman <测试>',
    kind: 'predicted',
    color: '#8b5cf6',
    points: [
      {
        time: 10,
        longitude: 20.01,
        latitude: 60.01,
        altitudeMetres: 3_000,
        groundSpeedMps: 100,
        horizontalErrorMetres: 100,
        segment: 'predicted',
      },
      {
        time: 20,
        longitude: 20.2,
        latitude: 60.2,
        altitudeMetres: 3_100,
        groundSpeedMps: 100,
        horizontalErrorMetres: 2_450,
        segment: 'predicted',
      },
    ],
  },
]

describe('comparisonReport', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('builds a self-contained escaped report from backend metrics', () => {
    const html = buildComparisonSnapshotReportHtml({
      artifact,
      snapshotId: 'flight-1 · 2026-09-22T00:00:00Z',
      tracks,
      metrics: [
        {
          methodId: 'kalman-cv',
          adeMetres: 1_275,
          fdeMetres: 2_450,
          maxErrorMetres: 2_450,
          worstTimeSeconds: 20,
        },
      ],
    })

    expect(html).toContain('<!doctype html>')
    expect(html).toContain('2.45 km')
    expect(html).toContain('+00:20')
    expect(html).toContain('Kalman &lt;测试&gt;')
    expect(html).toContain('<svg')
    expect(html).toContain('gbar-align-v1')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('private-thread-must-not-leak')
  })

  it('downloads the report with a safe filename and revokes its object URL', () => {
    vi.useFakeTimers()
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:report')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)

    downloadComparisonSnapshotReport({
      artifact,
      snapshotId: 'flight/1 · cutoff',
      tracks,
      metrics: [],
    })

    expect(createObjectUrl).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    expect(document.querySelector('a[download]')).toBeNull()
    vi.runAllTimers()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:report')
  })
})
