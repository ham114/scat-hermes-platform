<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useTrajectoryCursor } from '@/composables/useTrajectoryCursor'
import { estimateAircraftAttitude, interpolateTrackAtTime } from '@/utils/trajectoryPlayback'
import { BASEMAPS, CesiumTrajectoryVisualizer } from '@/visualization/CesiumTrajectoryVisualizer'
import type { Track3D, TrajectoryErrorSelection, TrajectoryViewPreset } from '@/visualization/types'

const props = defineProps<{
  tracks: Track3D[]
  errorSelection?: TrajectoryErrorSelection | null
  dimension?: '2d' | '3d'
}>()

const store = useTrajectoryCursor()
const container = ref<HTMLElement>()
const rootEl = ref<HTMLElement>()
const basemapFailed = ref(false)
const basemap = ref(BASEMAPS[0]!.id)
const followEnabled = ref(false)
const followTargetId = ref('')
const altitudeScale = ref(1)
const viewPreset = ref<TrajectoryViewPreset>('oblique')
const isFullscreen = ref(false)
const fullscreenError = ref('')
const staticOnly = computed(
  () => props.tracks.length > 0 && props.tracks.every((t) => t.timeBasis === 'sequence'),
)
let visualizer: CesiumTrajectoryVisualizer | null = null
let resizeObserver: ResizeObserver | undefined
let resizeFrame = 0

function resizeMap() {
  cancelAnimationFrame(resizeFrame)
  resizeFrame = requestAnimationFrame(() => visualizer?.resize?.())
}

const legendItems = computed(() =>
  props.tracks.map((track) => ({
    id: track.id,
    label: track.label,
    color: track.color,
    altitudeMetres:
      track.timeBasis === 'sequence'
        ? null
        : (interpolateTrackAtTime(track, store.currentTime)?.altitudeMetres ?? null),
    highlighted: props.errorSelection?.trackId === track.id,
  })),
)

const attitudeReadout = computed(() => {
  const track =
    props.tracks.find((item) => item.id === followTargetId.value) ?? props.tracks[0] ?? null
  if (!track || props.dimension === '2d' || track.timeBasis === 'sequence') return null
  const estimate = estimateAircraftAttitude(track, store.currentTime)
  if (!estimate) return null
  return {
    label: track.label,
    heading: ((estimate.headingRadians * 180) / Math.PI + 360) % 360,
    pitch: (estimate.pitchRadians * 180) / Math.PI,
    roll: (estimate.rollRadians * 180) / Math.PI,
    source: estimate.source,
  }
})

function formatAltitude(value: number | null): string {
  return value === null ? '高度未知' : `${Math.round(value).toLocaleString('zh-CN')} m`
}

function changeBasemap(id: string) {
  basemapFailed.value = false
  basemap.value = id
  visualizer?.setBasemap(id)
}

function toggleFollow() {
  followEnabled.value = !followEnabled.value
  visualizer?.setFollowTarget(followTargetId.value || null)
  visualizer?.setFollow(followEnabled.value)
}

function changeFollowTarget(id: string) {
  followTargetId.value = id
  visualizer?.setFollowTarget(id || null)
}

function changeAltitudeScale(multiplier: number) {
  altitudeScale.value = multiplier
  visualizer?.setAltitudeScale(multiplier)
}

function changeViewPreset(preset: TrajectoryViewPreset) {
  viewPreset.value = preset
  followEnabled.value = false
  visualizer?.setFollow(false)
  visualizer?.setViewPreset(preset)
}

async function toggleFullscreen() {
  fullscreenError.value = ''
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen()
    } else if (rootEl.value) {
      await rootEl.value.requestFullscreen()
    }
  } catch {
    fullscreenError.value = '浏览器暂不允许全屏，请重试。'
  }
}

function locateTracks() {
  followEnabled.value = false
  visualizer?.setFollow(false)
  visualizer?.setViewPreset(props.dimension === '2d' ? 'top' : viewPreset.value)
}

function onFullscreenChange() {
  isFullscreen.value = document.fullscreenElement === rootEl.value
  resizeMap()
}

onMounted(() => {
  if (!container.value) return
  document.addEventListener('fullscreenchange', onFullscreenChange)
  visualizer = new CesiumTrajectoryVisualizer(
    BASEMAPS[0]!.id,
    () => {
      basemapFailed.value = true
    },
    props.dimension ?? '3d',
  )
  visualizer.mount(container.value)
  if (typeof ResizeObserver !== 'undefined') {
    let lastSize = ''
    resizeObserver = new ResizeObserver(([entry]) => {
      if (!entry) return
      const size = `${Math.round(entry.contentRect.width)}:${Math.round(entry.contentRect.height)}`
      if (size === lastSize) return
      lastSize = size
      resizeMap()
    })
    resizeObserver.observe(container.value)
  }
  visualizer.setTracks(props.tracks)
  visualizer.setFollowTarget(followTargetId.value || null)
  visualizer.setAltitudeScale(altitudeScale.value)
  visualizer.setViewPreset(props.dimension === '2d' ? 'top' : viewPreset.value)
  visualizer.setTime(store.currentTime)
  visualizer.setHighlightTrack(props.errorSelection?.trackId ?? null)
})

watch(
  () => props.tracks,
  (tracks) => {
    if (!tracks.some((track) => track.id === followTargetId.value)) {
      followTargetId.value =
        tracks.find((track) => track.id === 'actual' || track.id.endsWith('::actual'))?.id ??
        tracks[0]?.id ??
        ''
    }
    visualizer?.setTracks(tracks)
    visualizer?.setFollowTarget(followTargetId.value || null)
    visualizer?.setTime(store.currentTime)
  },
  { immediate: true },
)

watch(
  () => props.errorSelection?.trackId,
  (trackId) => visualizer?.setHighlightTrack(trackId ?? null),
)

watch(
  () => store.currentTime,
  (time) => visualizer?.setTime(time),
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  cancelAnimationFrame(resizeFrame)
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  visualizer?.dispose()
  visualizer = null
})
</script>

<template>
  <section ref="rootEl" class="trajectory-3d">
    <el-alert
      v-if="basemapFailed"
      class="basemap-alert"
      type="warning"
      :closable="false"
      title="底图瓦片加载失败，已切换为无底图模式，轨迹回放不受影响"
    />
    <div class="map-shell">
      <div ref="container" class="cesium-container" />
      <aside v-if="legendItems.length > 0" class="map-legend" aria-label="三维轨迹图例">
        <strong>{{ staticOnly ? '静态轨迹' : '轨迹 / 当前高度' }}</strong>
        <ul>
          <li v-for="item in legendItems" :key="item.id" :class="{ highlighted: item.highlighted }">
            <span class="legend-swatch" :style="{ backgroundColor: item.color }" />
            <span class="legend-label">{{ item.label }}</span>
            <span v-if="!staticOnly" class="legend-altitude">{{
              formatAltitude(item.altitudeMetres)
            }}</span>
          </li>
        </ul>
      </aside>
    </div>
    <div v-if="$slots.playback" class="playback-slot">
      <slot name="playback" />
    </div>
    <div v-if="tracks.length" class="view-bar">
      <el-button
        v-if="!staticOnly && dimension !== '2d'"
        size="small"
        :type="followEnabled ? 'primary' : 'default'"
        @click="toggleFollow"
      >
        {{ followEnabled ? '跟随中' : '跟随视角' }}
      </el-button>
      <el-select
        v-if="!staticOnly && dimension !== '2d'"
        :teleported="false"
        :model-value="followTargetId"
        size="small"
        class="follow-target-select"
        aria-label="跟随目标"
        @update:model-value="changeFollowTarget"
      >
        <el-option v-for="track in tracks" :key="track.id" :label="track.label" :value="track.id" />
      </el-select>
      <el-select
        v-if="dimension !== '2d'"
        :teleported="false"
        :model-value="viewPreset"
        size="small"
        class="view-preset-select"
        aria-label="三维视角"
        @update:model-value="changeViewPreset"
      >
        <el-option label="斜视全景" value="oblique" />
        <el-option label="正上方俯视" value="top" />
        <el-option label="侧视高度" value="side" />
      </el-select>
      <el-select
        v-if="dimension !== '2d'"
        :teleported="false"
        :model-value="altitudeScale"
        size="small"
        class="altitude-scale-select"
        aria-label="高度显示倍率"
        @update:model-value="changeAltitudeScale"
      >
        <el-option label="高度 1×（真实）" :value="1" />
        <el-option label="高度 2×" :value="2" />
        <el-option label="高度 5×" :value="5" />
        <el-option label="高度 10×" :value="10" />
      </el-select>
      <el-select
        :teleported="false"
        :model-value="basemap"
        size="small"
        class="basemap-select"
        @update:model-value="changeBasemap"
      >
        <el-option v-for="item in BASEMAPS" :key="item.id" :label="item.label" :value="item.id" />
      </el-select>
      <el-button size="small" @click="toggleFullscreen">
        {{ isFullscreen ? '退出全屏' : '全屏' }}
      </el-button>
      <el-button size="small" @click="locateTracks">定位轨迹</el-button>
    </div>
    <p v-if="fullscreenError" role="status">{{ fullscreenError }}</p>
    <div v-if="attitudeReadout" class="attitude-readout" aria-label="当前姿态估计">
      <strong>{{ attitudeReadout.label }} · 姿态估计</strong>
      <span>航向 {{ attitudeReadout.heading.toFixed(1) }}°</span>
      <span>俯仰 {{ attitudeReadout.pitch.toFixed(1) }}°</span>
      <span>滚转 {{ attitudeReadout.roll.toFixed(1) }}°</span>
      <small>
        {{
          attitudeReadout.source === 'velocity' ? '六特征速度分量估算' : '相邻轨迹点反推'
        }}，仅用于回放
      </small>
    </div>
    <p v-if="altitudeScale > 1" class="height-note">
      高度显示已放大 {{ altitudeScale }}×，仅影响当前画面。
    </p>
  </section>
</template>

<style scoped>
.trajectory-3d {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-height: 480px;
}

.trajectory-3d:fullscreen {
  width: 100%;
  height: 100dvh;
  background: #0b1220;
  padding: 1rem;
}

.trajectory-3d:fullscreen .map-shell {
  min-height: 0;
}

.basemap-alert {
  flex: 0 0 auto;
}

.map-shell {
  position: relative;
  flex: 1 1 auto;
  min-height: 420px;
}

.cesium-container {
  position: absolute;
  inset: 0;
  border-radius: 8px;
  overflow: hidden;
  background: #0b1220;
}

.map-legend {
  position: absolute;
  z-index: 2;
  top: 0.75rem;
  right: 0.75rem;
  max-width: min(24rem, calc(100% - 1.5rem));
  border: 1px solid rgb(143 167 186 / 35%);
  border-radius: 6px;
  padding: 0.65rem 0.75rem;
  color: #e6f1f8;
  background: rgb(11 18 32 / 82%);
  backdrop-filter: blur(6px);
}

.map-legend ul {
  display: grid;
  gap: 0.35rem;
  margin: 0.5rem 0 0;
  padding: 0;
  list-style: none;
}

.map-legend li {
  display: grid;
  grid-template-columns: 0.65rem minmax(5rem, 1fr) auto;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
}

.map-legend li.highlighted {
  border-radius: 4px;
  outline: 1px solid #ef4444;
  outline-offset: 2px;
}

.legend-swatch {
  width: 0.65rem;
  height: 0.65rem;
  border: 1px solid rgb(255 255 255 / 55%);
  border-radius: 999px;
}

.legend-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.legend-altitude {
  color: #9bdcf2;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.view-bar {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.playback-slot {
  flex: 0 0 auto;
}

.basemap-select {
  width: 12rem;
}

.follow-target-select,
.view-preset-select,
.altitude-scale-select {
  width: 11rem;
}

.height-note {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.attitude-readout {
  display: flex;
  align-items: baseline;
  gap: 0.9rem;
  flex-wrap: wrap;
  border-radius: 6px;
  padding: 0.55rem 0.75rem;
  color: #dbeafe;
  background: #17243a;
  font-size: 0.82rem;
  font-variant-numeric: tabular-nums;
}

.attitude-readout strong {
  color: #fff;
}

.attitude-readout small {
  margin-left: auto;
  color: #93a4bb;
}

@media (max-width: 720px) {
  .map-legend {
    right: 0.5rem;
    top: 0.5rem;
  }
}
</style>
