<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  readDiagnostics,
  stateLabel,
  timestamp,
  duration,
  tokenUsage,
  summarizeTokenUsage,
  type DiagnosticTrace,
  type DiagnosticNode,
  type DiagnosticDetail,
} from '@/api/diagnostics'

const route = useRoute()
const displayName = (name: string) =>
  ({
    'Hermes turn': '处理本轮请求',
    'Tool: mcp__scat__get_scat_track_points': '查询航班轨迹点',
    'Tool: mcp__scat__search_scat_flights': '查询航班',
    'Tool: mcp__scat__diagnose_scat_track': '诊断轨迹质量',
    'Tool: mcp__scat__diagnose_uav_channels': '诊断 UAV 通道',
    'Tool: mcp__scat__diagnose_scat_prediction_errors': '分析已有预测误差',
    'Tool: skill_view': '读取技能',
  })[name] ?? name
const fieldLabel = (key: string) =>
  ({
    status: '业务状态',
    counts: '返回数量',
    keys: '返回字段',
    validation_fields: '校验失败的参数',
    batch: '数据批次',
    external_id: '航班编号',
    limit: '读取上限',
    start: '起始位置',
    result_id: '回执编号',
    payload_sha256: '结果摘要',
    pagination: '分页范围',
    operation: '操作',
    tool_call_id: '工具调用编号',
    tool_name: '工具名称',
    error_type: '错误类型',
    finish_reason: '结束原因',
  })[key] ?? key
const router = useRouter()
const rows = ref<DiagnosticTrace[]>([])
const detail = ref<DiagnosticDetail>()
const next = ref<string | null>(null)
const selected = ref('')
const tab = ref('overview')
const failuresOnly = ref(false)
const loading = ref(false)
const error = ref('')
let controller: AbortController | undefined
let generation = 0
const date = (value: number | string | null) =>
  value == null ? '未记录' : new Date(timestamp(value)).toLocaleString('zh-CN', { hour12: false })
const status = computed(() => String(route.query.state ?? ''))
const traceId = computed(() => String(route.params.traceId ?? ''))
const taskId = computed(() => String(route.params.taskId ?? ''))
const nodes = computed(() => {
  const all = (detail.value?.nodes ?? [])
    .slice()
    .sort((a, b) => timestamp(a.started) - timestamp(b.started))
  const index = new Set(all.map((node) => node.id))
  const seen = new Set<string>()
  const ordered: (DiagnosticNode & { depth: number })[] = []
  function visit(node: DiagnosticNode, depth: number) {
    if (seen.has(node.id)) return
    seen.add(node.id)
    if (!failuresOnly.value || node.state === 'failed') ordered.push({ ...node, depth })
    for (const child of all.filter((value) => value.parent_id === node.id)) visit(child, depth + 1)
  }
  for (const node of all.filter((value) => !value.parent_id || !index.has(value.parent_id)))
    visit(node, 0)
  for (const node of all) visit(node, 0)
  return ordered
})
const active = computed(() => detail.value?.nodes.find((node) => node.id === selected.value))
const firstFailure = computed(() => nodes.value.find((node) => node.state === 'failed'))
const range = computed(() => {
  const all = detail.value?.nodes ?? []
  const times = all
    .flatMap((node) => [timestamp(node.started), timestamp(node.ended ?? node.started)])
    .filter(Number.isFinite)
  return times.length
    ? { start: Math.min(...times), length: Math.max(1, Math.max(...times) - Math.min(...times)) }
    : { start: 0, length: 1 }
})
const usage = computed(() => summarizeTokenUsage(detail.value?.nodes ?? []))
const activeUsage = computed(() => (active.value ? tokenUsage(active.value) : null))
const cost = computed(() => {
  const generations = detail.value?.nodes.filter((node) => node.kind === 'generation') ?? []
  if (!generations.length || generations.some((node) => !Object.keys(node.cost ?? {}).length))
    return null
  return generations.reduce(
    (sum, node) =>
      sum +
      (node.cost?.total ??
        Object.entries(node.cost ?? {}).reduce(
          (n, [key, value]) => n + (key === 'total' ? 0 : value),
          0,
        )),
    0,
  )
})
function bar(node: DiagnosticNode) {
  if (!Number.isFinite(timestamp(node.started))) return { marginLeft: '0', width: '0' }
  return {
    marginLeft: `${(100 * (timestamp(node.started) - range.value.start)) / range.value.length}%`,
    width: `${Math.max(1, (100 * (duration(node) ?? 0)) / range.value.length)}%`,
  }
}
async function load(more = false) {
  controller?.abort()
  controller = new AbortController()
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    if (traceId.value || taskId.value) {
      const result = await readDiagnostics<DiagnosticDetail>(
        taskId.value
          ? '/tasks/' + encodeURIComponent(taskId.value)
          : '/' + encodeURIComponent(traceId.value),
        controller.signal,
      )
      if (current !== generation) return
      detail.value = result
      selected.value =
        result.nodes.find((node) => node.state === 'failed')?.id ?? result.nodes[0]?.id ?? ''
    } else {
      const query = new URLSearchParams({ limit: '20' })
      if (status.value) query.set('state', status.value)
      const thread = route.params.sessionId ?? route.query.thread_id
      if (typeof thread === 'string') query.set('thread_id', thread)
      if (typeof route.query.run_id === 'string') query.set('run_id', route.query.run_id)
      if (more && next.value) query.set('cursor', next.value)
      const result = await readDiagnostics<{
        items: DiagnosticTrace[]
        next_cursor: string | null
      }>('?' + query, controller.signal)
      if (current !== generation) return
      rows.value = more ? [...rows.value, ...result.items] : result.items
      next.value = result.next_cursor
      detail.value = undefined
    }
  } catch (failure) {
    if (current === generation && !controller.signal.aborted)
      error.value = failure instanceof Error ? failure.message : '读取失败。'
  } finally {
    if (current === generation) loading.value = false
  }
}
watch(
  () => route.fullPath,
  () => {
    detail.value = undefined
    rows.value = []
    void load()
  },
  { immediate: true },
)
onUnmounted(() => {
  generation++
  controller?.abort()
})
function filter(event: Event) {
  void router.replace({
    query: { ...route.query, state: (event.target as HTMLSelectElement).value || undefined },
  })
}
function selectNode(node: DiagnosticNode) {
  selected.value = node.id
  tab.value = 'overview'
}
</script>

<template>
  <section class="diagnostics" aria-label="调用链排查">
    <header class="toolbar">
      <div>
        <h1>
          {{
            taskId
              ? '后台任务调用记录'
              : traceId
                ? '调用链详情'
                : route.params.sessionId
                  ? '会话调用记录'
                  : '调用链排查'
          }}
        </h1>
        <p v-if="route.params.sessionId" class="muted">{{ route.params.sessionId }}</p>
      </div>
      <RouterLink v-if="traceId || taskId || route.params.sessionId" to="/diagnostics"
        >全部记录</RouterLink
      >
      <label v-if="!traceId && !taskId"
        >状态
        <select :value="status" @change="filter">
          <option value="">全部状态</option>
          <option
            v-for="state in [
              'running',
              'completed',
              'failed',
              'cancelled',
              'interrupted',
              'partial',
            ]"
            :key="state"
            :value="state"
          >
            {{ stateLabel(state) }}
          </option>
        </select></label
      >
      <button :disabled="loading" @click="load()">{{ loading ? '读取中…' : '刷新' }}</button>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="detail">
      <p v-if="detail.trace.label">{{ detail.trace.label }}</p>
      <div class="summary">
        <span :class="['state', detail.trace.state]">{{ stateLabel(detail.trace.state) }}</span>
        <span>{{ detail.trace.profile ?? '角色待关联' }}</span>
        <span>{{ date(detail.trace.created) }}</span>
        <span v-if="usage.state === 'no_generation'">暂无模型调用记录（工具步骤不计 token）</span>
        <span v-else-if="usage.state === 'missing'"
          >{{ usage.generations }} 次模型调用，用量尚未上报</span
        >
        <span v-else
          >模型用量 {{ usage.total }} tokens{{
            usage.state === 'partial' ? '（已知部分）' : ''
          }}</span
        >
        <span>费用估算 {{ cost == null ? '无法估算' : `$${cost.toFixed(5)}` }}</span>
      </div>
      <p v-if="usage.known" class="muted">
        输入 {{ usage.input }} · 输出 {{ usage.output }} · 输入中缓存命中 {{ usage.cached }} ·
        缓存写入 {{ usage.written }} · 已有用量 {{ usage.known }}/{{ usage.generations }} 次
      </p>
      <p class="muted" role="status">
        {{
          detail.langfuse_status === 'available'
            ? '已读取 Langfuse 记录'
            : detail.langfuse_status === 'pending'
              ? '等待 Langfuse 同步记录'
              : detail.langfuse_status === 'truncated'
                ? 'Langfuse 记录达到本页上限，当前展示部分步骤'
                : 'Langfuse 暂时不可用，当前显示本地记录'
        }}
        · {{ detail.complete ? '执行已结束' : '记录可能尚未完整' }}
      </p>
      <p v-if="firstFailure" class="failure">发现失败步骤 · {{ displayName(firstFailure.name) }}</p>
      <div v-if="detail.related_tasks.length" class="toolbar">
        <RouterLink
          v-for="task in detail.related_tasks"
          :key="task"
          :to="`/diagnostics/tasks/${task}`"
          >关联后台任务 {{ task.slice(0, 8) }}</RouterLink
        >
      </div>
      <details class="identifiers">
        <summary>会话与运行编号</summary>
        <dl>
          <dt>运行</dt>
          <dd>{{ detail.trace.run_id }}</dd>
          <dt>请求</dt>
          <dd>{{ detail.trace.request_id ?? '未关联' }}</dd>
          <dt>会话</dt>
          <dd>
            <RouterLink
              v-if="detail.trace.thread_id"
              :to="`/diagnostics/sessions/${detail.trace.thread_id}`"
              >{{ detail.trace.thread_id }}</RouterLink
            ><span v-else>未关联</span>
          </dd>
        </dl>
      </details>
      <label class="only-errors"
        ><input v-model="failuresOnly" type="checkbox" />仅看失败步骤</label
      >
      <div class="inspection">
        <div class="node-list" aria-label="调用步骤与耗时">
          <button
            v-for="node in nodes"
            :key="`${node.source}:${node.id}`"
            class="node"
            :style="{ paddingLeft: `${12 + Math.min(node.depth, 5) * 14}px` }"
            :class="{ selected: selected === node.id }"
            @click="selectNode(node)"
          >
            <span :class="['state', node.state]">{{ stateLabel(node.state) }}</span>
            <strong>{{ displayName(node.name) }}</strong>
            <small
              >{{ node.source === 'langfuse' ? 'Hermes' : '平台' }} ·
              {{
                duration(node) == null ? '耗时未记录' : `${(duration(node)! / 1000).toFixed(3)} s`
              }}</small
            >
            <span v-if="node.parent_id" class="muted parent"
              >上游 ·
              {{
                detail.nodes.find((item) => item.id === node.parent_id)?.name ?? '未取得上游记录'
              }}</span
            >
            <span class="timeline" aria-hidden="true"
              ><i :class="node.state" :style="bar(node)"></i
            ></span>
          </button>
          <p v-if="!nodes.length">
            {{ failuresOnly ? '当前记录未发现失败步骤。' : '尚未收到步骤记录。' }}
          </p>
        </div>
        <article v-if="active" class="node-detail" aria-label="步骤详情">
          <h2>{{ displayName(active.name) }}</h2>
          <section v-if="active.failure" class="failure-detail" aria-label="错误详情">
            <h3>{{ active.failure.title }}</h3>
            <p v-if="active.failure.message">{{ active.failure.message }}</p>
            <ul v-if="active.failure.validation?.length">
              <li v-for="(issue, index) in active.failure.validation" :key="index">
                {{ issue.field.join('.') || '请求参数' }}：{{ issue.message }}（{{ issue.code }}）
              </li>
            </ul>
            <p>处理建议：{{ active.failure.suggestion }}</p>
            <dl>
              <dt>错误代码</dt>
              <dd>{{ active.failure.code }}</dd>
              <template v-if="active.failure.exception_type"
                ><dt>异常类型</dt>
                <dd>{{ active.failure.exception_type }}</dd></template
              >
              <template v-if="active.failure.http_status"
                ><dt>HTTP 状态</dt>
                <dd>{{ active.failure.http_status }}</dd></template
              >
              <template v-if="active.failure.error_id"
                ><dt>日志定位编号</dt>
                <dd>{{ active.failure.error_id }}</dd></template
              >
            </dl>
            <details v-if="active.failure.frames?.length">
              <summary>代码调用位置</summary>
              <p v-for="(frame, index) in active.failure.frames" :key="index">
                {{ frame.file }}:{{ frame.line }} · {{ frame.function }}
              </p>
            </details>
          </section>
          <p v-if="active.kind === 'generation' && activeUsage?.total != null" class="muted">
            本次模型：输入 {{ activeUsage.input }} · 输出 {{ activeUsage.output }} · 总计
            {{ activeUsage.total }} · 缓存命中 {{ activeUsage.cached }}
            <span v-if="activeUsage.reasoning != null">
              · 输出中推理 {{ activeUsage.reasoning }}</span
            >
          </p>
          <p v-else-if="active.kind === 'generation'" class="muted">
            本次模型调用尚无完整输入/输出用量。
          </p>
          <div class="tabs" role="group" aria-label="详情内容">
            <button
              v-for="item in [
                { key: 'overview', name: '概况' },
                { key: 'input', name: '输入摘要' },
                { key: 'output', name: '输出摘要' },
              ]"
              :key="item.key"
              :aria-pressed="tab === item.key"
              @click="tab = item.key"
            >
              {{ item.name }}
            </button>
          </div>
          <dl v-if="tab === 'overview'">
            <dt>状态</dt>
            <dd>{{ stateLabel(active.state) }}</dd>
            <dt>开始时间</dt>
            <dd>{{ date(active.started) }}</dd>
            <dt>结束时间</dt>
            <dd>{{ active.ended == null ? '未记录' : date(active.ended) }}</dd>
            <dt>模型</dt>
            <dd>{{ active.model ?? '不适用或未记录' }}</dd>
            <dt>节点编号</dt>
            <dd>{{ active.id }}</dd>
            <dt>原始名称</dt>
            <dd>{{ active.name }}</dd>
            <template v-for="(value, key) in active.details ?? {}" :key="key"
              ><dt>{{ fieldLabel(String(key)) }}</dt>
              <dd>{{ value }}</dd></template
            >
          </dl>
          <template v-else>
            <dl v-if="Object.keys(active[tab === 'input' ? 'input' : 'output']).length">
              <template
                v-for="(value, key) in active[tab === 'input' ? 'input' : 'output']"
                :key="key"
                ><dt>{{ key }}</dt>
                <dd>{{ typeof value === 'object' ? JSON.stringify(value) : value }}</dd></template
              >
            </dl>
            <p v-else>此节点仅采集结构与用量，未取得内容摘要。</p>
          </template>
          <details>
            <summary>原始结构</summary>
            <pre>{{ JSON.stringify(active, null, 2) }}</pre>
          </details>
        </article>
      </div>
    </template>
    <div v-else-if="rows.length" class="records">
      <table>
        <thead>
          <tr>
            <th>开始时间</th>
            <th>请求摘要</th>
            <th>Agent</th>
            <th>状态</th>
            <th>运行</th>
            <th>用户</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>
              <RouterLink :to="`/diagnostics/traces/${row.id}`">{{ date(row.created) }}</RouterLink>
            </td>
            <td>{{ row.label ?? '结构化任务' }}</td>
            <td>{{ row.profile ?? '待关联' }}</td>
            <td>
              <span :class="['state', row.state]">{{ stateLabel(row.state) }}</span>
            </td>
            <td class="run-id">{{ row.run_id }}</td>
            <td>{{ row.owner }}</td>
          </tr>
        </tbody>
      </table>
      <button v-if="next" :disabled="loading" @click="load(true)">加载更多</button>
    </div>
    <p v-else-if="!loading && !error && !traceId && !taskId">当前筛选条件下没有采集记录。</p>
  </section>
</template>

<style scoped>
.diagnostics {
  min-width: 0;
  max-width: 1400px;
  margin: auto;
  padding: 20px;
  overflow-wrap: anywhere;
}
.toolbar,
.summary,
.tabs {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.toolbar {
  justify-content: space-between;
  margin-bottom: 16px;
}
h1 {
  margin: 0;
  font-size: 1.4rem;
}
h2 {
  font-size: 1.1rem;
}
.muted,
small {
  color: var(--muted, #697586);
}
.summary,
.identifiers,
.node-detail {
  padding: 16px;
  border: 1px solid var(--border, #d5dce5);
  border-radius: 10px;
}
.state {
  display: inline-block;
  font-size: 0.85rem;
  color: #475569;
}
.state.failed,
.failure {
  color: #b42318;
}
.failure-detail {
  padding: 12px;
  margin-bottom: 16px;
  border-left: 3px solid #c6574d;
  background: var(--panel, white);
}
.state.completed,
.state.succeeded {
  color: #166534;
}
.inspection {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(280px, 1fr);
  gap: 20px;
  align-items: start;
  margin-top: 12px;
}
.node {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  text-align: left;
  gap: 7px 12px;
  width: 100%;
  box-sizing: border-box;
  margin-bottom: 8px;
  padding: 12px;
  background: var(--panel, white);
  border: 1px solid var(--border, #d5dce5);
  border-radius: 8px;
}
.node-list,
.node-detail {
  min-width: 0;
}
.node.selected {
  outline: 2px solid #4464d7;
  outline-offset: -2px;
}
.node small,
.parent,
.timeline {
  grid-column: 1 / -1;
}
.timeline {
  background: #eef1f5;
  height: 5px;
  overflow: hidden;
  border-radius: 3px;
}
.timeline i {
  display: block;
  height: 100%;
  background: #6680d6;
}
.timeline i.failed {
  background: #c6574d;
}
.only-errors {
  display: block;
  margin-top: 16px;
}
dl {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  gap: 10px;
  font-size: 0.9rem;
}
dd {
  margin: 0;
  overflow-wrap: anywhere;
}
dt {
  color: var(--muted, #697586);
}
details {
  margin-top: 16px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 0.8rem;
  max-height: 450px;
  overflow: auto;
}
.records {
  overflow: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
}
td,
th {
  text-align: left;
  padding: 12px;
  border-bottom: 1px solid var(--border, #d5dce5);
}
.run-id {
  overflow-wrap: anywhere;
  max-width: 220px;
  font-size: 0.85rem;
}
@media (max-width: 760px) {
  .diagnostics {
    padding: 12px;
  }
  .inspection {
    grid-template-columns: minmax(0, 1fr);
  }
  .summary {
    gap: 8px;
    font-size: 0.85rem;
  }
}
</style>
