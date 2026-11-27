<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { HermesApiError, newRequestId } from '@/api/hermes'
import {
  listVisualizations,
  visualizationSources,
  createVisualization,
  reviseVisualization,
  cancelVisualization,
  type VisualizationTask,
  type VisualizationSource,
} from '@/api/visualizations'
import VisualizationArtifactView from './VisualizationArtifactView.vue'
const props = defineProps<{ threadId?: string; revision?: number; readOnly?: boolean }>()
const emit = defineEmits<{ available: [value: boolean] }>()
const items = ref<VisualizationTask[]>([])
const enabled = ref(false)
const error = ref('')
const acting = ref(false)
const sources = ref<VisualizationSource[]>([])
const selected = ref('')
const intent = ref('')
const showCreate = ref(false)
type Pending =
  | { thread: string; request_id: string; intent: string; data_refs: VisualizationSource[] }
  | { thread: string; request_id: string; artifact: string; feedback: string }
const pending = ref<Pending>()
const pendingKey = (thread: string) => 'scat-vis-pending-v1:' + thread
function restorePending(thread?: string): Pending | undefined {
  if (!thread) return
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingKey(thread)) ?? 'null') as Pending | null
    if (!value || value.thread !== thread || !/^[a-f0-9-]{36}$/i.test(value.request_id)) return
    if ('artifact' in value) {
      if (
        /^[a-f0-9]{32}$/.test(value.artifact) &&
        typeof value.feedback === 'string' &&
        value.feedback.length <= 4000
      )
        return value
    } else if (
      typeof value.intent === 'string' &&
      value.intent.length <= 8000 &&
      Array.isArray(value.data_refs) &&
      value.data_refs.length > 0 &&
      value.data_refs.length <= 16 &&
      value.data_refs.every(
        (ref) =>
          ref &&
          ['readonly_result', 'analysis_artifact'].includes(ref.kind) &&
          typeof ref.source_id === 'string' &&
          typeof ref.run_id === 'string' &&
          /^[a-f0-9]{64}$/.test(ref.payload_sha256),
      )
    )
      return value
  } catch {
    /* Recovery storage is optional; history GETs remain available. */
  }
}
watch(pending, (value) => {
  try {
    if (value) sessionStorage.setItem(pendingKey(value.thread), JSON.stringify(value))
    else if (props.threadId) sessionStorage.removeItem(pendingKey(props.threadId))
  } catch {
    /* The current in-memory request ID is still retained. */
  }
})
let generation = 0
let controller: AbortController | undefined
let timer: ReturnType<typeof setTimeout> | undefined
const labels: Record<string, string> = {
  queued: '排队中',
  generating: '编写绘图代码',
  rendering: '绘制图表',
  reviewing: '审核图表',
  revising: '根据审核修改',
  publishing: '保存图表',
  succeeded: '已交付',
  failed: '未通过或执行失败',
  cancelled: '已取消',
  needs_reconciliation: '执行状态待核实',
}
async function load() {
  const thread = props.threadId
  const token = ++generation
  clearTimeout(timer)
  controller?.abort()
  controller = new AbortController()
  if (!thread) return
  try {
    const result = await listVisualizations(thread, controller.signal)
    if (token !== generation || thread !== props.threadId) return
    if (!Array.isArray(result.items)) return
    items.value = result.items
    enabled.value = result.enabled
    emit(
      'available',
      items.value.some((t) => t.state === 'succeeded'),
    )
    if (items.value.some((t) => ['queued', 'running'].includes(t.state)))
      timer = setTimeout(() => void load(), 2000)
  } catch {
    if (token === generation && items.value.length) error.value = '任务状态暂时不可用，请刷新查询。'
  }
}
watch(
  () => props.threadId,
  () => {
    items.value = []
    sources.value = []
    enabled.value = false
    showCreate.value = false
    pending.value = restorePending(props.threadId)
    error.value = ''
    emit('available', false)
    void load()
  },
  { immediate: true },
)
watch(
  () => props.revision,
  () => void load(),
)
onUnmounted(() => {
  generation++
  clearTimeout(timer)
  controller?.abort()
})
async function prepare() {
  const thread = props.threadId
  if (!thread) return
  showCreate.value = !showCreate.value
  if (!showCreate.value) return
  try {
    const result = await visualizationSources(thread)
    if (thread === props.threadId) {
      sources.value = result.items
      selected.value = result.items[0]?.source_id ?? ''
    }
  } catch {
    if (thread === props.threadId) error.value = '未能读取来源，请先在会话中查询真实数据。'
  }
}
async function submit() {
  if (props.readOnly || acting.value || !pending.value) return
  const request = pending.value
  if (request.thread !== props.threadId) return
  acting.value = true
  error.value = ''
  try {
    if ('artifact' in request)
      await reviseVisualization(request.thread, request.artifact, {
        request_id: request.request_id,
        feedback: request.feedback,
      })
    else
      await createVisualization(request.thread, {
        request_id: request.request_id,
        intent: request.intent,
        data_refs: request.data_refs.map(({ kind, source_id, run_id, payload_sha256 }) => ({
          kind,
          source_id,
          run_id,
          payload_sha256,
        })),
      })
    if (request.thread === props.threadId) {
      pending.value = undefined
      showCreate.value = false
      await load()
    }
  } catch (cause) {
    if (request.thread !== props.threadId) return
    const unknown = !(cause instanceof HermesApiError) || cause.outcomeUnknown
    if (!unknown) pending.value = undefined
    error.value = unknown
      ? '入队结果尚未确认。请先刷新任务；恢复请求会使用原请求号。'
      : (cause as Error).message
  } finally {
    acting.value = false
  }
}
function create() {
  const source = sources.value.find((s) => s.source_id === selected.value)
  if (!source || !props.threadId || pending.value || !intent.value.trim()) return
  pending.value = {
    thread: props.threadId,
    request_id: newRequestId(),
    intent: intent.value.trim(),
    data_refs: [source],
  }
  void submit()
}
function revise(artifact: string, feedback: string) {
  if (!props.threadId || pending.value || acting.value) return
  pending.value = { thread: props.threadId, request_id: newRequestId(), artifact, feedback }
  void submit()
}
async function cancel(task: string) {
  if (!props.threadId || props.readOnly || acting.value) return
  const thread = props.threadId
  acting.value = true
  try {
    await cancelVisualization(thread, task)
    if (thread === props.threadId) await load()
  } catch {
    if (thread === props.threadId) error.value = '取消结果尚未确认，请刷新状态。'
  } finally {
    acting.value = false
  }
}
</script>

<template>
  <section
    v-if="items.length || (enabled && !readOnly)"
    class="visualization-tasks"
    aria-label="自主图表任务"
  >
    <header>
      <h2>自主图表</h2>
      <button @click="load">刷新图表任务</button
      ><button v-if="enabled && !readOnly" :disabled="acting || !!pending" @click="prepare">
        根据会话数据绘图
      </button>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="pending" :disabled="acting || readOnly" @click="submit">恢复原绘图请求</button>
    <form v-if="showCreate && !readOnly" @submit.prevent="create">
      <label
        >数据来源<select v-model="selected" aria-label="绘图数据来源">
          <option v-for="s in sources" :key="s.source_id" :value="s.source_id">
            {{ s.operation }} · {{ s.source_id.slice(0, 8) }}
          </option>
        </select></label
      >
      <p v-if="!sources.length">请先在会话中查询需要绘制的数据。</p>
      <label
        >绘图要求<textarea v-model="intent" aria-label="自主绘图要求" maxlength="8000" />
      </label>
      <button :disabled="acting || !!pending || !selected || !intent.trim()">创建图表</button>
    </form>
    <article v-for="task in items" :key="task.task_id">
      <p>
        版本 {{ task.version }} ·
        {{ labels[task.state === 'running' ? task.phase : task.state] ?? task.state }}
      </p>
      <template v-if="task.state !== 'succeeded'">
        <p>{{ task.intent }}</p>
        <p v-if="task.error_code">图表未交付（{{ task.error_code }}）。已发布版本仍可查看。</p>
        <button
          v-if="['queued', 'running'].includes(task.state)"
          :disabled="acting || readOnly"
          @click="cancel(task.task_id)"
        >
          取消绘图
        </button>
      </template>
      <VisualizationArtifactView
        v-if="threadId && task.state === 'succeeded' && task.artifact_id"
        :thread-id="threadId"
        :artifact-id="task.artifact_id"
        :read-only="readOnly || !enabled || acting || !!pending"
        @revise="revise"
      />
    </article>
  </section>
</template>

<style scoped>
.visualization-tasks {
  min-width: 0;
  padding: 16px 0;
}
header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
h2 {
  font-size: 18px;
}
p {
  overflow-wrap: anywhere;
}
label,
textarea,
select {
  display: block;
  max-width: 100%;
}
textarea {
  width: 100%;
  box-sizing: border-box;
  min-height: 72px;
}
form,
article {
  margin: 12px 0;
}
button,
select,
textarea {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 12px;
  font: inherit;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
label {
  margin: 10px 0;
}
</style>
