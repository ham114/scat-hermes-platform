<script setup lang="ts">
import { computed } from 'vue'

import type { SnapshotMethodMetric, Track3D, TrajectoryErrorSelection } from '@/visualization/types'

const props = defineProps<{
  tracks: Track3D[]
  metrics: SnapshotMethodMetric[]
  selection?: TrajectoryErrorSelection | null
}>()
const emit = defineEmits<{ locate: [selection: TrajectoryErrorSelection] }>()

function methodId(trackId: string): string {
  return trackId.includes('::') ? trackId.slice(trackId.lastIndexOf('::') + 2) : trackId
}

const rows = computed(() =>
  props.tracks
    .filter((track) => track.kind === 'predicted')
    .map((track) => {
      const worstPoint = track.points.reduce<(typeof track.points)[number] | null>(
        (worst, point) =>
          typeof point.horizontalErrorMetres === 'number' &&
          (worst === null || point.horizontalErrorMetres > (worst.horizontalErrorMetres ?? -1))
            ? point
            : worst,
        null,
      )
      if (!worstPoint || typeof worstPoint.horizontalErrorMetres !== 'number') return null
      const metric = props.metrics.find((item) => item.methodId === methodId(track.id))
      return {
        track,
        time: metric?.worstTimeSeconds ?? worstPoint.time,
        errorMetres: metric?.maxErrorMetres ?? worstPoint.horizontalErrorMetres,
        adeMetres: metric?.adeMetres ?? null,
        fdeMetres: metric?.fdeMetres ?? null,
      }
    })
    .filter((row): row is NonNullable<typeof row> => row !== null)
    .sort((left, right) => right.errorMetres - left.errorMetres),
)

function formatDistance(value: number | null): string {
  if (value === null) return '—'
  return value >= 1_000
    ? `${(value / 1_000).toFixed(2)} km`
    : `${Math.round(value).toLocaleString('zh-CN')} m`
}

function formatTime(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  return `+${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}
</script>

<template>
  <section v-if="rows.length > 0" class="worst-error-panel" aria-label="最差误差点定位">
    <header>
      <h3>最差误差点</h3>
    </header>
    <div class="error-table-wrap">
      <table>
        <thead>
          <tr>
            <th>方法</th>
            <th>最大误差</th>
            <th>相对时刻</th>
            <th>ADE</th>
            <th>FDE</th>
            <th><span class="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(row, index) in rows"
            :key="row.track.id"
            :class="{ selected: selection?.trackId === row.track.id }"
          >
            <td>
              <span class="method-name">
                <span class="swatch" :style="{ backgroundColor: row.track.color }" />
                {{ row.track.label }}
                <span v-if="index === 0" class="global-worst">全局最差</span>
              </span>
            </td>
            <td>{{ formatDistance(row.errorMetres) }}</td>
            <td>{{ formatTime(row.time) }}</td>
            <td>{{ formatDistance(row.adeMetres) }}</td>
            <td>{{ formatDistance(row.fdeMetres) }}</td>
            <td>
              <el-button
                size="small"
                :type="selection?.trackId === row.track.id ? 'primary' : 'default'"
                @click="
                  emit('locate', {
                    trackId: row.track.id,
                    time: row.time,
                    horizontalErrorMetres: row.errorMetres,
                  })
                "
              >
                {{ selection?.trackId === row.track.id ? '已定位' : '定位' }}
              </el-button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.worst-error-panel {
  display: grid;
  gap: 0.65rem;
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

.error-table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
}

th,
td {
  border-bottom: 1px solid var(--line-soft);
  padding: 0.55rem 0.65rem;
  text-align: right;
  white-space: nowrap;
}

th:first-child,
td:first-child {
  text-align: left;
}

tr.selected {
  background: color-mix(in srgb, var(--cyan) 10%, transparent);
}

.method-name {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}

.swatch {
  width: 0.65rem;
  height: 0.65rem;
  border-radius: 50%;
}

.global-worst {
  border-radius: 999px;
  padding: 0.1rem 0.35rem;
  color: #ffd6d6;
  background: rgb(190 24 93 / 28%);
  font-size: 0.7rem;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}
</style>
