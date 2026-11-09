<script setup lang="ts">
import { computed, defineAsyncComponent, provide, watch } from 'vue'
import { createPinia } from 'pinia'
import { useTimeCursorStore } from '@/stores/timeCursor'
import { trajectoryCursorKey } from '@/composables/useTrajectoryCursor'
import { buildTrajectoryRenderData } from '@/utils/trackNormalizer'
import type { TrajectoryBlock } from '@/lib/analysisDisplay'

const MapView = defineAsyncComponent(() => import('./Trajectory3dView.vue'))
const props = defineProps<{ block: TrajectoryBlock }>()
const cursor = useTimeCursorStore(createPinia())
provide(trajectoryCursorKey, cursor)
const data = computed(() =>
  buildTrajectoryRenderData([props.block], { includeUntimedGeometry: true }),
)
watch(
  () => data.value.tracks,
  (tracks) => cursor.setTracks(tracks),
  { immediate: true },
)
</script>
<template>
  <section aria-label="二维街道轨迹地图">
    <MapView v-if="data.tracks.length" :tracks="data.tracks" dimension="2d" />
    <p v-else>当前查询没有有效的地理坐标。</p>
    <p v-if="data.tracks.some((t) => t.timeBasis === 'sequence')">
      未提供有效时间，按原始点序显示静态轨迹。
    </p>
  </section>
</template>
