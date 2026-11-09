<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  listRunResults,
  readRunResult,
  readTrajectoryView,
  type ResultReference,
  type TrajectoryViewCoverage,
} from '@/api/workspace'
import AnalysisResultRenderer from './AnalysisResultRenderer.vue'
import { parseAnalysisArtifact } from '@/lib/analysisDisplay'
import {
  presentReceipts,
  receiptName,
  receiptRange,
  type ReceiptView,
} from '@/lib/receiptPresentation'
const props = defineProps<{
  threadId: string
  runId: string
  status?: string
  autoPlot?: boolean
}>()
const emptyNotice = computed(() => {
  if (props.status === '执行失败')
    return '本次指令执行失败，没有可读取的只读数据。此前成功查询的数据仍可从对应历史回复读取。'
  if (props.status === '对话已停止') return '本次指令已停止，尚未产生可读取的只读数据。'
  if (props.status === '对话已中断') return '本次指令已中断，尚未产生可读取的只读数据。'
  return '本次回复没有独立的只读数据回执。模型目录、任务状态等信息请查看回复或任务面板。'
})
const partial = computed(() =>
  ['执行失败', '对话已停止', '对话已中断'].includes(props.status ?? ''),
)
const open = ref(false),
  loading = ref(false),
  error = ref('')
const items = ref<ResultReference[]>([])
const sources = ref<ReceiptView[]>([])
const selected = ref('')
const mapCoverage = ref<TrajectoryViewCoverage>()
const mapScope = ref('')
function sourceScope(source?: ReceiptView): string {
  const query = (source?.data as { query?: Record<string, unknown> } | undefined)?.query ?? {}
  return JSON.stringify([
    query.batch,
    query.external_id,
    query.start_time ?? null,
    query.end_time ?? null,
  ])
}
const isDisplayOnly = (source: ReceiptView) =>
  Boolean((source.data as { display_only?: boolean })?.display_only)
const presentations = computed(() => {
  const viewScopes = new Set(sources.value.filter(isDisplayOnly).map(sourceScope))
  return presentReceipts(
    sources.value.filter(
      (s) =>
        !viewScopes.has(sourceScope(s)) ||
        s.reference.operation !== 'get_scat_track_points' ||
        isDisplayOnly(s),
    ),
  )
})
const canExpand = computed(() => items.value.some((r) => r.trajectory_view_available === true))
const current = computed(
  () => presentations.value.find((p) => p.id === selected.value) ?? presentations.value[0],
)
let generation = 0
let control: AbortController | undefined
function toggle() {
  open.value = !open.value
  if (open.value) void load()
}
async function load() {
  const token = ++generation
  control?.abort()
  control = new AbortController()
  loading.value = true
  error.value = ''
  sources.value = []
  mapCoverage.value = undefined
  selected.value = ''
  try {
    const data = await listRunResults(props.threadId, props.runId, control.signal)
    if (token === generation) items.value = data
  } catch {
    if (token === generation) error.value = '来源暂时不可用或已过期。'
  } finally {
    if (token === generation) loading.value = false
  }
  return token === generation
}
async function read(expand = props.autoPlot && canExpand.value) {
  let preferredScope = current.value ? sourceScope(current.value.sources[0]) : ''
  const token = ++generation
  control?.abort()
  control = new AbortController()
  const { threadId, runId } = props
  const signal = control.signal
  const refs = items.value.slice(0, 64)
  loading.value = true
  error.value = ''
  sources.value = []
  const values: ReceiptView[] = []
  let failures = 0,
    next = 0
  // Bounded concurrent GETs, no new business query or model request.
  await Promise.all(
    Array.from({ length: Math.min(4, refs.length) }, async () => {
      while (next < refs.length && !signal.aborted) {
        const item = refs[next++]!
        try {
          const value = await readRunResult(threadId, runId, item, signal)
          const analysis = parseAnalysisArtifact(
            { artifact_id: item.result_id, analysis_result: value.analysis_result },
            item.result_id,
          )
          if (
            analysis &&
            (analysis.context?.readonly_result_id !== item.result_id ||
              analysis.context?.readonly_payload_sha256 !== item.payload_sha256)
          )
            throw new Error('来源不匹配')
          values.push({ reference: item, data: value.result, analysis })
        } catch {
          failures++
        }
      }
    }),
  )
  if (expand && token === generation) {
    const eligible = values.filter(
      (s) => s.reference.operation === 'get_scat_track_points' && !isDisplayOnly(s) && s.analysis,
    )
    const seed = eligible.find((s) => sourceScope(s) === preferredScope) ?? eligible[0]
    if (seed) {
      try {
        const view = await readTrajectoryView(threadId, runId, seed.reference, signal)
        if (token !== generation) return
        if (view.message) error.value = view.message
        const addedSources: ReceiptView[] = []
        const addedReferences: ResultReference[] = []
        for (const ref of view.items) {
          if (values.some((s) => s.reference.result_id === ref.result_id)) continue
          const value = await readRunResult(threadId, runId, ref, signal)
          const analysis = parseAnalysisArtifact(
            { artifact_id: ref.result_id, analysis_result: value.analysis_result },
            ref.result_id,
          )
          if (
            !analysis ||
            analysis.context?.readonly_result_id !== ref.result_id ||
            analysis.context?.readonly_payload_sha256 !== ref.payload_sha256
          )
            throw new Error('来源不匹配')
          addedSources.push({ reference: ref, data: value.result, analysis })
          addedReferences.push(ref)
        }
        if (token !== generation) return
        values.push(...addedSources)
        refs.push(...addedReferences)
        if (view.items.length) {
          mapCoverage.value = view.coverage
          mapScope.value = sourceScope(seed)
          if (!preferredScope) preferredScope = mapScope.value
        }
        items.value = [...refs]
      } catch {
        if (token === generation) error.value = '全程轨迹暂时不可用，保留已读取的分析数据。'
      }
    }
  }
  if (token !== generation) return
  sources.value = values.sort(
    (a, b) =>
      refs.findIndex((r) => r.result_id === a.reference.result_id) -
      refs.findIndex((r) => r.result_id === b.reference.result_id),
  )
  selected.value =
    (
      presentations.value.find((p) => sourceScope(p.sources[0]) === preferredScope) ??
      (props.autoPlot
        ? presentations.value.find((p) => p.analysis.blocks.some((b) => b.type === 'trajectory_2d'))
        : undefined) ??
      presentations.value[0]
    )?.id ?? ''
  if (failures) error.value = `来源暂时不可用或已过期（${failures}份）；仅展示成功读取的部分。`
  if (items.value.length > refs.length) error.value += '来源较多，本次仅加载前64份。'
  loading.value = false
}
watch(
  () => [props.threadId, props.runId],
  () => {
    generation++
    control?.abort()
    items.value = []
    sources.value = []
    selected.value = ''
    error.value = ''
    open.value = false
    loading.value = false
  },
)
watch(
  () => [props.threadId, props.runId, props.autoPlot, props.status],
  async () => {
    if (!props.autoPlot || props.status !== '已完成') return
    const loaded = await load()
    if (loaded && items.value.length && props.autoPlot && props.status === '已完成') {
      open.value = true
      await read(canExpand.value)
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  generation++
  control?.abort()
})
</script>
<template>
  <div class="run-evidence">
    <button class="message-detail-button" :aria-expanded="open" @click="toggle">
      查看来源 <span aria-hidden="true">{{ open ? '−' : '+' }}</span>
    </button>
    <section v-if="open" class="run-evidence-body" aria-label="结果来源">
      <p v-if="loading" role="status">正在读取…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="!loading && !error && !items.length" role="status">{{ emptyNotice }}</p>
      <div v-if="items.length" class="evidence-item">
        <p>本轮 {{ items.length }} 份数据来源，用于同一次绘图或分析。</p>
        <button class="text-button" :disabled="loading" @click="read()">查看数据</button>
        <button
          v-if="
            canExpand &&
            sources.length &&
            (!mapCoverage || (current && sourceScope(current.sources[0]) !== mapScope))
          "
          :disabled="loading"
          @click="read(true)"
        >
          查看全程轨迹
        </button>
        <p v-if="mapCoverage && (!current || sourceScope(current.sources[0]) === mapScope)">
          地图展示 {{ mapCoverage.loaded }}/{{ mapCoverage.total }} 点，{{
            mapCoverage.complete
              ? '完整逐点轨迹'
              : mapCoverage.full_extent
                ? mapCoverage.sampling_mode === 'segmented'
                  ? '全程分段抽样，保留首末点'
                  : `全程抽样（每${mapCoverage.every_nth}点抽1点，保留首末点）`
                : '部分范围'
          }}；文字分析依据仍以回复中的系统核验范围为准。
        </p>
        <p v-if="partial" role="status">
          本次指令未完整完成，以下仅为已取得的数据，不代表分析已完成。
        </p>
        <template v-if="sources.length">
          <section v-if="current" aria-label="本轮主结果">
            <label v-if="presentations.length > 1"
              >展示范围
              <select v-model="selected" aria-label="展示范围">
                <option v-for="p in presentations" :key="p.id" :value="p.id">{{ p.label }}</option>
              </select>
            </label>
            <p>{{ current.note }}</p>
            <p>
              图表范围以本次查询参数和返回统计为准。分页样本仅代表已加载范围；点数、抽样与下一页状态见窗口指标和来源明细。
            </p>
            <AnalysisResultRenderer
              :key="current.id"
              :result="current.analysis"
              :display-identity="
                current.sources.length > 1
                  ? current.sources
                      .map((s) => s.reference.result_id + ':' + s.reference.payload_sha256)
                      .join(';')
                  : undefined
              "
              :artifact-id="
                current.sources.length === 1 ? current.sources[0]!.reference.result_id : undefined
              "
              :payload-sha256="
                current.sources.length === 1
                  ? current.sources[0]!.reference.payload_sha256
                  : undefined
              "
            />
          </section>
          <details class="source-details" :open="!presentations.length">
            <summary>数据来源明细 · {{ sources.length }} 份（取数记录）</summary>
            <article v-for="(source, i) in sources" :key="source.reference.result_id">
              <h4>来源 {{ i + 1 }} · {{ receiptName(source.reference.operation) }}</h4>
              <p>{{ receiptRange(source) }}</p>
              <details>
                <summary>原始查询数据</summary>
                <pre>{{ JSON.stringify(source.data, null, 2) }}</pre>
              </details>
            </article>
          </details>
        </template>
      </div>
    </section>
  </div>
</template>
<style scoped>
.source-details {
  margin-top: 1rem;
}
.source-details article {
  border-top: 1px solid var(--border-color, #d1d5db);
  padding: 0.5rem 0;
}
pre {
  max-height: 24rem;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
select {
  max-width: 100%;
}
</style>
