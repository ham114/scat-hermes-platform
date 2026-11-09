<script setup lang="ts">
import type { EChartsCoreOption } from 'echarts/core'
import { computed } from 'vue'

import BaseChart from '@/components/BaseChart.vue'
import { useTrajectoryCursor } from '@/composables/useTrajectoryCursor'
import { buildTrackPlayback2D } from '@/utils/trajectoryPlayback'
import type { Track3D, TrajectoryErrorSelection } from '@/visualization/types'

const props = defineProps<{
  tracks: Track3D[]
  errorSelection?: TrajectoryErrorSelection | null
}>()
const store = useTrajectoryCursor()

function formatSeconds(seconds: number): string {
  const sign = seconds < 0 ? '-' : ''
  const total = Math.abs(Math.round(seconds))
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${sign}${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

const chartOption = computed<EChartsCoreOption>(() => {
  const series: Array<Record<string, unknown>> = []
  for (const track of props.tracks) {
    const playback = buildTrackPlayback2D(track, store.currentTime)
    series.push(
      {
        name: `${track.label} · 完整路径`,
        type: 'line',
        data: playback.fullPath,
        showSymbol: false,
        silent: true,
        lineStyle: { color: track.color, width: 1, opacity: 0.25, type: 'dashed' },
      },
      {
        name: `${track.label} · 已播放`,
        type: 'line',
        data: playback.trail,
        showSymbol: false,
        lineStyle: { color: track.color, width: 3, opacity: 0.95 },
      },
    )
    if (playback.current) {
      series.push({
        name: `${track.label} · 当前`,
        type: 'scatter',
        data: [[playback.current.longitude, playback.current.latitude]],
        symbolSize: 11,
        itemStyle: { color: track.color, borderColor: '#ffffff', borderWidth: 2 },
        z: 10,
      })
    }
  }
  const selection = props.errorSelection
  const selectedTrack = selection
    ? props.tracks.find((track) => track.id === selection.trackId)
    : undefined
  const selectedPoint = selectedTrack
    ? selectedTrack.points.find((point) => point.time === selection?.time)
    : undefined
  if (selection && selectedTrack && selectedPoint) {
    series.push({
      name: `${selectedTrack.label} · 最大误差`,
      type: 'scatter',
      data: [[selectedPoint.longitude, selectedPoint.latitude]],
      symbol: 'diamond',
      symbolSize: 20,
      itemStyle: { color: '#ef4444', borderColor: '#ffffff', borderWidth: 2 },
      label: {
        show: true,
        position: 'top',
        color: '#f8fafc',
        formatter: `最大误差 ${(selection.horizontalErrorMetres / 1_000).toFixed(2)} km`,
      },
      z: 20,
    })
  }
  return {
    animation: false,
    legend: {
      type: 'scroll',
      top: 0,
      textStyle: { color: '#8fa7ba' },
      data: props.tracks.map((track) => `${track.label} · 已播放`),
    },
    grid: { left: 70, right: 30, top: 48, bottom: 58 },
    tooltip: {
      trigger: 'item',
      formatter: (parameters: unknown) => {
        const item = parameters as { seriesName?: string; value?: [number, number] }
        const value = item.value
        if (!Array.isArray(value)) return item.seriesName ?? ''
        return `${item.seriesName ?? ''}<br/>经度 ${value[0].toFixed(5)}°<br/>纬度 ${value[1].toFixed(5)}°`
      },
    },
    xAxis: {
      type: 'value',
      name: '经度 (°)',
      nameLocation: 'middle',
      nameGap: 34,
      scale: true,
      axisLabel: { color: '#8fa7ba' },
      axisLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.35)' } },
      splitLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.12)' } },
    },
    yAxis: {
      type: 'value',
      name: '纬度 (°)',
      nameLocation: 'middle',
      nameGap: 48,
      scale: true,
      axisLabel: { color: '#8fa7ba' },
      axisLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.35)' } },
      splitLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.12)' } },
    },
    series,
  }
})
</script>

<template>
  <section class="trajectory-2d-playback" aria-label="二维轨迹时间轴回放">
    <header>
      <h3>二维轨迹</h3>
      <strong class="cursor-readout" data-testid="two-dimensional-cursor">
        {{ formatSeconds(store.currentTime - store.minTime) }} /
        {{ formatSeconds(store.duration) }}
      </strong>
    </header>
    <BaseChart
      :option="chartOption"
      height="360px"
      label="二维经纬度轨迹：虚线为完整路径，实线为当前时刻前的已播放路径"
    />
  </section>
</template>

<style scoped>
.trajectory-2d-playback {
  display: grid;
  gap: 0.5rem;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  padding: 1rem;
  background: color-mix(in srgb, var(--surface-raised) 72%, transparent);
}

header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}

h3 {
  margin: 0;
}

h3 {
  font-size: 1.05rem;
}

.cursor-readout {
  color: var(--cyan);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
</style>
