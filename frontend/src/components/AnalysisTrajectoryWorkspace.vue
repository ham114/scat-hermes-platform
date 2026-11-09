<script setup lang="ts">
import { computed, defineAsyncComponent, provide, ref, watch } from 'vue'
import { createPinia } from 'pinia'
import { useTimeCursorStore } from '@/stores/timeCursor'
import { trajectoryCursorKey } from '@/composables/useTrajectoryCursor'
import { buildTrajectoryRenderData } from '@/utils/trackNormalizer'
import type { TrajectoryBlock } from '@/lib/analysisDisplay'
import Trajectory2dPlayback from './Trajectory2dPlayback.vue'
import TrajectoryPlaybackControls from './TrajectoryPlaybackControls.vue'
import TrajectoryProfileCharts from './TrajectoryProfileCharts.vue'
const Trajectory3dView = defineAsyncComponent(() => import('./Trajectory3dView.vue'))
const props = defineProps<{ block: TrajectoryBlock }>()
const cursor = useTimeCursorStore(createPinia())
provide(trajectoryCursorKey, cursor)
const mode = ref('2d'),
  snapshot = ref('')
const data = computed(() =>
  buildTrajectoryRenderData([props.block], { includeUntimedGeometry: true }),
)
const snapshots = computed(() => [...new Set(data.value.tracks.map((x) => x.snapshotId))])
watch(
  snapshots,
  (values) => {
    snapshot.value = values[0] ?? ''
  },
  { immediate: true },
)
const tracks = computed(() => data.value.tracks.filter((x) => x.snapshotId === snapshot.value))
const playable = computed(
  () => tracks.value.length > 0 && tracks.value.every((t) => t.timeBasis !== 'sequence'),
)
const coordinates = ref(false)
watch(tracks, (values) => cursor.setTracks(values), { immediate: true })
</script>
<template>
  <section aria-label="轨迹交互工作台">
    <label
      >快照<select v-model="snapshot" aria-label="交互快照">
        <option v-for="value in data.snapshots" :key="value.id" :value="value.id">
          {{ value.label }}
        </option>
      </select></label
    >
    <button @click="mode = mode === '2d' ? '3d' : '2d'">
      {{ mode === '2d' ? '切换三维' : '切换二维' }}
    </button>
    <TrajectoryPlaybackControls
      v-if="playable"
      v-model:selected-snapshot-id="snapshot"
      :tracks="tracks"
      :snapshots="data.snapshots"
    />
    <Trajectory3dView
      v-if="tracks.length"
      :key="mode"
      :tracks="tracks"
      :dimension="mode === '2d' ? '2d' : '3d'"
    />
    <p v-else>当前查询没有有效的地理坐标。</p>
    <p v-if="!playable && tracks.length">
      未提供有效时间，显示静态轨迹；不生成时间回放或姿态估计。
    </p>
    <button v-if="playable && mode === '2d'" @click="coordinates = !coordinates">
      {{ coordinates ? '收起坐标图' : '经纬度坐标图' }}
    </button>
    <Trajectory2dPlayback v-if="playable && mode === '2d' && coordinates" :tracks="tracks" />
    <TrajectoryProfileCharts v-if="playable" :tracks="tracks" />
    <p>
      源点 {{ data.downsampling.sourcePointCount }} · 展示点
      {{ data.downsampling.renderedPointCount }}
    </p>
  </section>
</template>
