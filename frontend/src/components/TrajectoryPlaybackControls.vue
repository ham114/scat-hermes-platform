<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'

import { PLAYBACK_SPEEDS } from '@/stores/timeCursor'
import { useTrajectoryCursor } from '@/composables/useTrajectoryCursor'
import type { Track3D, TrajectorySnapshot } from '@/visualization/types'

const props = defineProps<{
  tracks: Track3D[]
  snapshots: TrajectorySnapshot[]
  selectedSnapshotId: string
}>()

const emit = defineEmits<{
  'update:selectedSnapshotId': [value: string]
}>()

const store = useTrajectoryCursor()
let rafId = 0
let lastTimestamp = 0

const snapshotId = computed({
  get: () => props.selectedSnapshotId,
  set: (value: string) => emit('update:selectedSnapshotId', value),
})

const sliderTime = computed({
  get: () => store.currentTime,
  set: (value: number) => store.setTime(value),
})

const playbackSpeed = computed({
  get: () => store.speed,
  set: (value: number) => store.setSpeed(value),
})

function formatSeconds(seconds: number): string {
  const total = Math.max(Math.round(seconds), 0)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

function frame(timestamp: number) {
  if (lastTimestamp === 0) lastTimestamp = timestamp
  const delta = Math.min((timestamp - lastTimestamp) / 1000, 0.1)
  lastTimestamp = timestamp
  store.advance(delta)
  rafId = requestAnimationFrame(frame)
}

watch(
  () => props.tracks,
  (tracks) => store.setTracks(tracks),
  { immediate: true },
)

onMounted(() => {
  rafId = requestAnimationFrame(frame)
})

onBeforeUnmount(() => {
  cancelAnimationFrame(rafId)
  store.pause()
})
</script>

<template>
  <section v-if="store.hasTracks" class="playback-controls" aria-label="共享轨迹播放控制">
    <label v-if="snapshots.length > 1" class="snapshot-control">
      <span>快照</span>
      <el-select v-model="snapshotId" size="small" data-testid="snapshot-select">
        <el-option
          v-for="snapshot in snapshots"
          :key="snapshot.id"
          :label="`${snapshot.label}（${snapshot.trackCount} 条）`"
          :value="snapshot.id"
        />
      </el-select>
    </label>
    <el-button size="small" data-testid="playback-toggle" @click="store.toggle()">
      {{ store.playing ? '暂停' : '播放' }}
    </el-button>
    <label class="speed-control">
      <span>倍速</span>
      <el-select v-model="playbackSpeed" size="small">
        <el-option
          v-for="speed in PLAYBACK_SPEEDS"
          :key="speed"
          :label="`${speed}×`"
          :value="speed"
        />
      </el-select>
    </label>
    <el-slider
      v-model="sliderTime"
      class="time-slider"
      :min="store.minTime"
      :max="store.maxTime"
      :step="1"
      size="small"
      aria-label="轨迹时间"
    />
    <span class="time-readout" data-testid="shared-time-readout">
      {{ formatSeconds(store.currentTime - store.minTime) }} /
      {{ formatSeconds(store.duration) }}
    </span>
  </section>
</template>

<style scoped>
.playback-controls {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 0.75rem;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: color-mix(in srgb, var(--surface-raised) 78%, transparent);
}

.snapshot-control,
.speed-control {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.snapshot-control :deep(.el-select) {
  width: 22rem;
  max-width: 55vw;
}

.speed-control :deep(.el-select) {
  width: 6rem;
}

.time-slider {
  flex: 1 1 12rem;
  min-width: 12rem;
}

.time-readout {
  color: var(--scat-text-secondary, #8b93a7);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
</style>
