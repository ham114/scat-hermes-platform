<script setup lang="ts">
import type { EChartsCoreOption } from 'echarts/core'
import { computed } from 'vue'

import BaseChart from '@/components/BaseChart.vue'
import { useTrajectoryCursor } from '@/composables/useTrajectoryCursor'
import { interpolateTrackAtTime } from '@/utils/trajectoryPlayback'
import type { Track3D, TrackPoint3D } from '@/visualization/types'

const props = defineProps<{ tracks: Track3D[] }>()
const store = useTrajectoryCursor()

type ProfileMetric = 'altitudeMetres' | 'groundSpeedMps'

function metricValue(point: TrackPoint3D, metric: ProfileMetric): number | null {
  return point[metric]
}

function buildProfileOption(metric: ProfileMetric, axisName: string): EChartsCoreOption {
  const series: Array<Record<string, unknown>> = []
  const legend: string[] = []
  for (const track of props.tracks) {
    const data = track.points.flatMap((point): Array<[number, number]> => {
      const value = metricValue(point, metric)
      return value === null ? [] : [[point.time, value]]
    })
    if (data.length === 0) continue
    const isFirstLine = series.length === 0
    legend.push(track.label)
    series.push({
      name: track.label,
      type: 'line',
      data,
      showSymbol: false,
      connectNulls: false,
      lineStyle: { color: track.color, width: 2 },
      itemStyle: { color: track.color },
      markLine: isFirstLine
        ? {
            silent: true,
            symbol: ['none', 'none'],
            label: { show: false },
            lineStyle: { color: '#67e8f9', width: 1, type: 'dashed' },
            data: [{ xAxis: store.currentTime }],
          }
        : undefined,
    })
    const current = interpolateTrackAtTime(track, store.currentTime)
    const currentValue = current ? metricValue(current, metric) : null
    if (currentValue !== null) {
      series.push({
        name: `${track.label} · 当前`,
        type: 'scatter',
        data: [[store.currentTime, currentValue]],
        symbolSize: 9,
        itemStyle: { color: track.color, borderColor: '#ffffff', borderWidth: 2 },
        z: 10,
      })
    }
  }
  return {
    animation: false,
    legend: {
      type: 'scroll',
      top: 0,
      data: legend,
      textStyle: { color: '#8fa7ba' },
    },
    grid: { left: 72, right: 28, top: 48, bottom: 56 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'value',
      name: '预测时刻 (s)',
      nameLocation: 'middle',
      nameGap: 32,
      min: store.minTime,
      max: store.maxTime,
      axisLabel: { color: '#8fa7ba' },
      axisLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.35)' } },
      splitLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.12)' } },
    },
    yAxis: {
      type: 'value',
      name: axisName,
      nameLocation: 'middle',
      nameGap: 52,
      scale: true,
      axisLabel: { color: '#8fa7ba' },
      axisLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.35)' } },
      splitLine: { lineStyle: { color: 'rgba(100, 142, 173, 0.12)' } },
    },
    series,
  }
}

const hasAltitude = computed(() =>
  props.tracks.some((track) => track.points.some((point) => point.altitudeMetres !== null)),
)
const hasGroundSpeed = computed(() =>
  props.tracks.some((track) => track.points.some((point) => point.groundSpeedMps !== null)),
)
const altitudeOption = computed(() => buildProfileOption('altitudeMetres', '高度 (m)'))
const groundSpeedOption = computed(() => buildProfileOption('groundSpeedMps', '地速 (m/s)'))
</script>

<template>
  <section class="trajectory-profile-charts" aria-label="高度与速度时间曲线">
    <header>
      <h3>高度与速度</h3>
      <strong data-testid="profile-time-cursor">游标 {{ store.currentTime.toFixed(1) }} s</strong>
    </header>
    <div class="profile-grid">
      <div class="profile-card">
        <h4>高度</h4>
        <BaseChart
          v-if="hasAltitude"
          :option="altitudeOption"
          height="300px"
          label="各轨迹高度随预测时刻变化曲线"
        />
        <p v-else class="empty-profile" data-testid="altitude-empty">当前结果未提供高度序列。</p>
      </div>
      <div class="profile-card">
        <h4>水平地速</h4>
        <BaseChart
          v-if="hasGroundSpeed"
          :option="groundSpeedOption"
          height="300px"
          label="各轨迹水平地速随预测时刻变化曲线"
        />
        <p v-else class="empty-profile" data-testid="ground-speed-empty">
          当前结果未提供速度序列。
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.trajectory-profile-charts {
  display: grid;
  gap: 0.75rem;
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

h3,
h4,
p {
  margin: 0;
}

h3 {
  font-size: 1.05rem;
}

h4 {
  font-size: 0.95rem;
}

header strong {
  color: var(--cyan);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.profile-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.profile-card {
  min-width: 0;
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  padding: 0.75rem;
}

.empty-profile {
  display: grid;
  min-height: 300px;
  place-items: center;
}

.empty-profile {
  color: var(--text-muted);
  font-size: 0.85rem;
}

@media (max-width: 900px) {
  .profile-grid {
    grid-template-columns: 1fr;
  }
}
</style>
