import { describe, expect, it } from 'vitest'

import type { BatchOverview, BatchSummary } from '@/api/client'
import { buildOverviewCsv } from './overviewExport'

describe('overview CSV export', () => {
  it('exports deterministic metrics and distributions', () => {
    const summary = {
      batch: 'batch-a',
      flight_count: 2,
      earliest_iobt_utc: '2016-10-20T00:00:00Z',
      latest_iobt_utc: '2016-10-21T00:00:00Z',
    } satisfies BatchSummary
    const overview = {
      statistics: {
        batch: 'batch-a',
        flight_count: 2,
        unique_callsign_count: 2,
        aircraft_type_count: 1,
        departure_airport_count: 1,
        arrival_airport_count: 1,
        total_track_points: 200,
        average_track_points: 100,
        minimum_track_points: 90,
        maximum_track_points: 110,
        flights_with_prediction: 1,
        total_prediction_snapshots: 8,
      },
      top_aircraft_types: [{ aircraft_type: 'A320', flight_count: 2 }],
      top_routes: [{ adep: 'ZBAA', ades: 'ZSSS', flight_count: 2 }],
      top_departure_airports: [{ airport: 'ZBAA', flight_count: 2 }],
      top_arrival_airports: [{ airport: 'ZSSS', flight_count: 2 }],
      flight_volume: [{ period_start: '2016-10-20T00:00:00Z', flight_count: 2 }],
      statistics_source: 'live',
      data_quality: {
        missing_callsign_count: 0,
        missing_aircraft_type_count: 0,
        missing_departure_airport_count: 0,
        missing_arrival_airport_count: 0,
        missing_iobt_count: 0,
        empty_trajectory_count: 0,
        flights_without_prediction_count: 1,
      },
    } satisfies BatchOverview

    const csv = buildOverviewCsv(summary, overview)

    expect(csv).toContain('航班数,2')
    expect(csv).toContain('A320,2')
    expect(csv).toContain('ZBAA-ZSSS,2')
    expect(csv).toContain('起飞机场,航班数')
    expect(csv).toContain('ZSSS,2')
  })
})
