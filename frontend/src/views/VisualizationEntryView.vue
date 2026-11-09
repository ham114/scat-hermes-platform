<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { getAnalysisArtifact, type AnalysisArtifactView } from '@/api/client'
import type { components } from '@/api/schema'
import AsyncStatePanel from '@/components/AsyncStatePanel.vue'
import Trajectory2dPlayback from '@/components/Trajectory2dPlayback.vue'
import TrajectoryPlaybackControls from '@/components/TrajectoryPlaybackControls.vue'
import TrajectoryProfileCharts from '@/components/TrajectoryProfileCharts.vue'
import TrajectoryWorstErrorPanel from '@/components/TrajectoryWorstErrorPanel.vue'
import { useTimeCursorStore } from '@/stores/timeCursor'
import { downloadComparisonSnapshotReport } from '@/utils/comparisonReport'
import { buildStreamlitUrl } from '@/utils/crossAppNavigation'
import { buildTrajectoryRenderData } from '@/utils/trackNormalizer'
import type { SnapshotMethodMetric, TrajectoryErrorSelection } from '@/visualization/types'

// Cesium 体量较大，按需懒加载，不进门户主包。
const Trajectory3dView = defineAsyncComponent(() => import('@/components/Trajectory3dView.vue'))

type AnalysisBlock = components['schemas']['AnalysisResult']['blocks'][number]

function isTrajectory2dBlock(
  block: AnalysisBlock,
): block is components['schemas']['Trajectory2dBlock'] {
  return block.type === 'trajectory_2d'
}

function isTableBlock(block: AnalysisBlock): block is components['schemas']['TableBlock'] {
  return block.type === 'table'
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

const route = useRoute()
const artifactId = computed(() => String(route.params.artifactId ?? ''))
const isValidArtifactId = computed(() =>
  /^([0-9a-f]{32}|[A-Za-z0-9][A-Za-z0-9._:-]{7,199})$/.test(artifactId.value),
)
const artifact = ref<AnalysisArtifactView>()
const loadError = ref<unknown>()
const loading = ref(false)
const selectedSnapshotId = ref('')
const errorSelection = ref<TrajectoryErrorSelection | null>(null)
const timeCursor = useTimeCursorStore()
const returnToChatUrl = computed(() => buildStreamlitUrl(artifact.value?.thread_id))
const resultTitle = computed(() => {
  if (!artifact.value) return '正在打开分析结果'
  if (artifact.value.analysis_result.operation === 'run_model_comparison') {
    return '四方法轨迹预测对比'
  }
  if (artifact.value.model_key === 'scat-gbar-v23') return 'GBAR 轨迹预测结果'
  return artifact.value.analysis_result.title.replace(/^[A-Za-z0-9._:-]+\s+/, '')
})
const resultModelLabel = computed(() => {
  if (!artifact.value) return ''
  if (artifact.value.analysis_result.operation === 'run_model_comparison') {
    return 'GBAR · Kalman · 匀速-恒航向 · CFPL'
  }
  if (artifact.value.model_key === 'scat-gbar-v23') return 'GBAR v23'
  return `${artifact.value.model_key}:${artifact.value.model_version}`
})

const renderData = computed(() => {
  if (!artifact.value) return buildTrajectoryRenderData([])
  const blocks = artifact.value.analysis_result.blocks.filter(isTrajectory2dBlock)
  return buildTrajectoryRenderData(blocks)
})

watch(
  () => renderData.value.snapshots,
  (snapshots) => {
    if (!snapshots.some((snapshot) => snapshot.id === selectedSnapshotId.value)) {
      selectedSnapshotId.value = snapshots[0]?.id ?? ''
    }
  },
  { immediate: true },
)

const tracks = computed(() =>
  renderData.value.tracks.filter((track) => track.snapshotId === selectedSnapshotId.value),
)

const snapshotMethodMetrics = computed<SnapshotMethodMetric[]>(() => {
  const table = artifact.value?.analysis_result.blocks
    .filter(isTableBlock)
    .find((block) => block.title === '可视化快照逐方法指标')
  if (!table) return []
  return (table.rows ?? [])
    .filter((row) => row.snapshot_id === selectedSnapshotId.value)
    .map((row) => ({
      methodId: String(row.method_id ?? ''),
      adeMetres: finiteNumber(row.ade_m),
      fdeMetres: finiteNumber(row.fde_m),
      maxErrorMetres: finiteNumber(row.max_error_m),
      worstTimeSeconds: finiteNumber(row.worst_time_s),
    }))
    .filter((row) => row.methodId.length > 0)
})

const canDownloadStaticReport = computed(
  () =>
    artifact.value?.analysis_result.operation === 'run_model_comparison' &&
    selectedSnapshotId.value.length > 0 &&
    tracks.value.length > 0 &&
    snapshotMethodMetrics.value.length > 0,
)

watch(selectedSnapshotId, () => {
  errorSelection.value = null
})

function locateWorstError(selection: TrajectoryErrorSelection) {
  errorSelection.value = selection
  timeCursor.pause()
  timeCursor.setTime(selection.time)
}

function downloadStaticReport() {
  if (!artifact.value || !canDownloadStaticReport.value) return
  downloadComparisonSnapshotReport({
    artifact: artifact.value,
    snapshotId: selectedSnapshotId.value,
    tracks: tracks.value,
    metrics: snapshotMethodMetrics.value,
  })
}

function formatDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

async function loadArtifact() {
  artifact.value = undefined
  loadError.value = undefined
  if (!isValidArtifactId.value) return
  loading.value = true
  try {
    artifact.value = await getAnalysisArtifact(artifactId.value)
  } catch (error) {
    loadError.value = error
  } finally {
    loading.value = false
  }
}

watch(artifactId, loadArtifact, { immediate: true })
</script>

<template>
  <section class="page flight-entry-page visualization-entry-page">
    <router-link class="back-link" to="/">← 返回系统入口</router-link>
    <article class="panel result-header-card">
      <div class="result-heading">
        <div>
          <h1>{{ resultTitle }}</h1>
          <p v-if="artifact" class="result-meta">
            <span>{{ resultModelLabel }}</span>
            <span>生成于 {{ formatDateTime(artifact.created_at) }}</span>
          </p>
          <p v-else-if="!isValidArtifactId" class="artifact-error">
            结果链接无效，请从对话中的结果按钮重新打开。
          </p>
        </div>
        <div class="result-actions">
          <a class="primary-link" :href="returnToChatUrl">
            {{ artifact ? '返回原对话' : '返回智能分析' }}
          </a>
          <el-button v-if="canDownloadStaticReport" plain @click="downloadStaticReport">
            导出当前快照
          </el-button>
        </div>
      </div>
      <AsyncStatePanel
        v-if="isValidArtifactId && (loading || loadError)"
        :state="loading ? 'loading' : 'error'"
        :error="loadError"
        loading-description="正在加载分析结果…"
        @retry="loadArtifact"
      />
    </article>

    <article v-if="tracks.length > 0" class="panel trajectory-panel">
      <h2>轨迹对比回放</h2>
      <Trajectory3dView :tracks="tracks" :error-selection="errorSelection">
        <template #playback>
          <TrajectoryPlaybackControls
            v-model:selected-snapshot-id="selectedSnapshotId"
            :tracks="tracks"
            :snapshots="renderData.snapshots"
          />
        </template>
      </Trajectory3dView>
      <TrajectoryWorstErrorPanel
        :tracks="tracks"
        :metrics="snapshotMethodMetrics"
        :selection="errorSelection"
        @locate="locateWorstError"
      />
      <Trajectory2dPlayback :tracks="tracks" :error-selection="errorSelection" />
      <TrajectoryProfileCharts :tracks="tracks" />
    </article>
  </section>
</template>

<style scoped>
.result-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1.5rem;
}

.result-heading h1 {
  margin: 0;
  font-size: clamp(1.6rem, 3vw, 2.15rem);
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1rem;
  margin: 0.55rem 0 0;
  color: var(--text-muted);
  font-size: 0.85rem;
}

.result-actions {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  flex: 0 0 auto;
}

@media (max-width: 1100px) {
  .result-heading {
    align-items: stretch;
    flex-direction: column;
  }

  .result-actions {
    flex-wrap: wrap;
  }
}
</style>
