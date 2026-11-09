<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import {
  confirmModelTask,
  getModelTaskArtifact,
  getSavedModelOutput,
  listModelTasks,
  revokeModelTask,
  reconcileModelTask,
  cancelModelTask,
} from '@/api/hermes'
import type { ModelTask } from '@/api/hermes'
import AnalysisResultRenderer from './AnalysisResultRenderer.vue'
import { parseAnalysisArtifact, type AnalysisResult } from '@/lib/analysisDisplay'

const props = defineProps<{
  threadId?: string
  revision?: number
  showEmpty?: boolean
  readOnly?: boolean
}>()
const items = ref<ModelTask[]>([])
const error = ref('')
const loading = ref(false)
const acting = ref(false)
const unresolved = ref(false)
let alive = true
let generation = 0
let previewGeneration = 0
const previewError = ref('')
const previewLoading = ref(false)
const preview = ref<{
  taskId: string
  artifactId: string
  digest: string
  result: AnalysisResult
}>()
async function showResult(task: ModelTask, artifactId?: string) {
  const tid = props.threadId
  if (!tid) return
  const current = ++previewGeneration
  previewLoading.value = true
  previewError.value = ''
  preview.value = undefined
  try {
    const data = artifactId
      ? await getModelTaskArtifact(tid, task.task_id, artifactId)
      : await getSavedModelOutput(tid, task.task_id)
    if (!alive || current !== previewGeneration || tid !== props.threadId) return
    const identity = artifactId ?? String(data.artifact_id ?? '')
    const result = parseAnalysisArtifact(data, identity)
    if (
      !result ||
      data.run_id !== task.run_id ||
      typeof data.payload_sha256 !== 'string' ||
      !/^[a-f0-9]{64}$/.test(data.payload_sha256)
    )
      throw new Error('Invalid analysis artifact')
    preview.value = {
      taskId: task.task_id,
      artifactId: identity,
      digest: data.payload_sha256,
      result,
    }
  } catch {
    if (alive && current === previewGeneration && tid === props.threadId)
      previewError.value = '结果暂时不可用、已过期或超出展示范围。可以刷新后重试，或下载完整结果。'
  } finally {
    if (alive && current === previewGeneration) previewLoading.value = false
  }
}
watch(
  () => props.threadId,
  () => {
    previewGeneration++
    preview.value = undefined
    previewError.value = ''
    previewLoading.value = false
  },
)
const labels: Record<string, string> = {
  pending: '等待你确认',
  expired: '确认已过期',
  revoked: '已撤销',
  queued: '已提交，等待 worker',
  claimed: '执行中',
  unknown: '状态待核实，禁止自动重跑',
  finished: '执行已结束',
  succeeded: '执行成功',
  failed: '执行失败',
  cancelled: '已取消',
  partial: '部分成功',
  running: '执行中',
  paused: '已暂停，保留进度与预算',
  cancel_requested: '正在停止并核实后台状态',
}
const groupLabels: Record<string, string> = {
  scat_data_steps: '固定样本',
  scat_materialization_steps: '数据物化',
  scat_experiment_steps: '模型与实验',
  scat_evaluation_steps: '评估诊断',
  scat_report_steps: '报告',
}
const operationLabels: Record<string, string> = {
  scat_sample_prepare: '读取并核对冻结输入',
  scat_trial_run: '执行已批准的模型推理',
  scat_materialize: '处理已批准的派生数据',
  scat_materialization_read: '核对数据处理结果',
  scat_evaluation_summarize: '汇总有效样本与评估',
  scat_evaluation_diagnose: '诊断覆盖、缺失及失败',
  scat_report_build: '生成结果报告',
  scat_report_read: '核对并读取最终结果',
}
async function control(task: ModelTask, cancel: boolean) {
  if (props.readOnly) return
  const tid = props.threadId
  if (!tid || acting.value || unresolved.value) return
  acting.value = true
  try {
    if (cancel) await cancelModelTask(tid, task.task_id)
    else await reconcileModelTask(tid, task.task_id)
    await refresh()
  } catch {
    if (tid === props.threadId) error.value = '后台证据尚未就绪，请稍后刷新。'
  } finally {
    acting.value = false
  }
}
async function refresh() {
  const tid = props.threadId
  const current = ++generation
  if (!tid) {
    items.value = []
    error.value = ''
    unresolved.value = false
    return
  }
  loading.value = true
  try {
    const page = await listModelTasks(tid)
    if (!Array.isArray(page.items)) throw new Error('Invalid task page')
    if (alive && current === generation && tid === props.threadId) {
      items.value = page.items
      unresolved.value = false
      error.value = ''
    }
  } catch {
    if (alive && current === generation)
      error.value = '任务状态暂时不可用，请刷新查询；不要重新发起相同计算。'
  } finally {
    if (alive && current === generation) loading.value = false
  }
}
async function act(task: ModelTask, confirm: boolean) {
  if (props.readOnly) return
  const tid = props.threadId
  if (!tid || acting.value || unresolved.value) return
  acting.value = true
  try {
    if (confirm) await confirmModelTask(tid, task)
    else await revokeModelTask(tid, task.task_id)
    await refresh()
  } catch {
    if (tid === props.threadId) {
      unresolved.value = true
      error.value = '操作结果尚未确认，请先刷新任务状态。原任务号已保留。'
    }
  } finally {
    acting.value = false
  }
}
async function download(task: ModelTask, artifactId: string) {
  const tid = props.threadId
  if (!tid) return
  try {
    const data = await getModelTaskArtifact(tid, task.task_id, artifactId)
    if (!alive || tid !== props.threadId) return
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = `analysis-${task.task_id}.json`
    link.click()
    URL.revokeObjectURL(url)
  } catch {
    error.value = '结果获取失败，请刷新后重试。'
  }
}
watch(
  () => [props.threadId, props.revision],
  () => {
    void refresh()
  },
  { immediate: true },
)
const timer = setInterval(() => {
  if (!loading.value && !acting.value && props.threadId) void refresh()
}, 5000)
onUnmounted(() => {
  alive = false
  previewGeneration++
  generation++
  clearInterval(timer)
})
</script>

<template>
  <section
    v-if="threadId && (items.length || error || showEmpty)"
    class="panel model-task-panel"
    aria-label="模型任务"
  >
    <div class="task-heading">
      <h2>会话任务</h2>
      <button type="button" class="secondary-link" :disabled="loading || acting" @click="refresh">
        刷新任务
      </button>
    </div>
    <p v-if="items.some((item) => item.state === 'pending')">
      核对参数后确认执行。停止对话不会取消计算。
    </p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!items.length && !error">当前会话尚无模型任务。</p>
    <article v-for="task in items" :key="task.task_id" class="task-card">
      <h3>
        {{ task.model.display_name
        }}<template v-if="!task.workflow"> · {{ task.model.version }}</template>
      </h3>
      <strong>{{
        labels[
          task.skill_job?.state ??
            (task.state === 'finished' ? (task.run?.status ?? 'finished') : task.state)
        ] ??
        labels[task.state] ??
        task.state
      }}</strong>
      <div v-if="task.workflow" class="workflow-summary">
        <p v-if="task.workflow.proposal.models?.length">
          模型：{{
            task.workflow.proposal.models
              .map((m) => (m.version ? `${m.model}:${m.version}` : m.model))
              .join(' / ')
          }}
        </p>
        <p v-if="task.workflow.gpu">
          GPU · {{ task.workflow.gpu.device }} · {{ task.workflow.gpu.release_id }}。
          GBAR使用冻结的原始窗口，固定预处理为36个输入时刻；图中观测为模型预处理结果。
        </p>
        <p>
          {{ task.workflow.synthetic ? '合成数据展示' : '业务数据' }} · 固定样本
          {{ task.workflow.coverage.selected_count }} 架 · 试验 {{ task.workflow.trials.length }} 项
        </p>
        <template v-if="task.workflow.prediction_contract">
          <p>
            {{
              task.workflow.prediction_contract.mode === 'future_forecast'
                ? '从末次观测向未来预测'
                : task.workflow.prediction_contract.mode === 'historical_backtest'
                  ? '历史回测'
                  : '数据处理'
            }}
            <template v-if="task.workflow.prediction_contract.observation_last_n">
              · 每个起点精确使用最后 {{ task.workflow.prediction_contract.observation_last_n }} 点
            </template>
          </p>
          <p v-if="task.workflow.prediction_contract.mode === 'future_forecast'">
            预测起点之后没有已知真值，不计算误差指标。
          </p>
          <p v-for="(window, index) in task.workflow.prediction_contract.windows" :key="index">
            航班 {{ window.external_id }} · {{ window.point_count }} 点 · 起点
            {{ window.forecast_origin }} · 观测跨度 {{ window.span_s }} 秒
            <template v-if="window.requested_as_of">
              · 请求截止 {{ window.requested_as_of }} · 末观测距截止
              {{ window.observation_lag_s }} 秒；预测时域从请求截止计算
            </template>
          </p>
        </template>
        <div class="tool-groups" aria-label="业务工具组">
          <span v-for="(_, group) in task.workflow.tool_groups" :key="group">{{
            groupLabels[group] ?? group
          }}</span>
        </div>
        <details>
          <summary>工具组与批准方案</summary>
          <pre>{{ JSON.stringify(task.workflow, null, 2) }}</pre>
        </details>
      </div>
      <p class="task-id">任务 {{ task.task_id }} · run {{ task.run_id }}</p>
      <details :open="task.state === 'pending'">
        <summary>核对数据范围、参数与版本</summary>
        <pre>{{
          JSON.stringify(
            {
              input_refs: task.input_refs,
              parameters: task.parameters,
              evaluator: task.evaluator,
              components: task.components,
              code_version: task.code_version,
              limits: task.model.limits,
            },
            null,
            2,
          )
        }}</pre>
        <small
          >确认有效期至
          {{
            new Date(task.expires_at * 1000).toLocaleString()
          }}。参数变更后需要重新准备和确认。</small
        >
      </details>
      <p v-if="task.run?.error">{{ task.run.error.message }}</p>
      <details v-if="task.skill_job" :open="task.skill_job.state === 'running'">
        <summary>持久步骤与累计预算</summary>
        <ul v-if="task.skill_job.steps?.length">
          <li v-for="(step, index) in task.skill_job.steps" :key="index">
            {{ operationLabels[step.operation] ?? step.operation }} ·
            {{ labels[step.state] ?? step.state }}
            <small v-if="step.error">{{ step.error }}</small>
          </li>
        </ul>
        <p v-if="task.skill_job.backend_executions?.length">
          独立计算单位 {{ task.skill_job.backend_executions.length }} · 实际执行
          {{ task.skill_job.backend_executions.reduce((n, b) => n + b.calls, 0) }} 次
        </p>
        <pre>{{ JSON.stringify(task.skill_job.budget, null, 2) }}</pre>
      </details>
      <p v-if="task.state === 'unknown'">
        执行结果尚未核实，请联系维护人员按 run 查询。系统不会自动重新执行此任务。
      </p>
      <div class="task-buttons">
        <button
          v-if="!readOnly && ['unknown', 'paused'].includes(task.skill_job?.state ?? '')"
          type="button"
          class="secondary-link"
          :disabled="acting || unresolved"
          @click="control(task, false)"
        >
          核实并恢复原任务
        </button>
        <button
          v-if="
            !readOnly &&
            ['running', 'unknown', 'paused', 'cancel_requested'].includes(
              task.skill_job?.state ?? '',
            )
          "
          type="button"
          class="secondary-link"
          :disabled="acting || unresolved"
          @click="control(task, true)"
        >
          取消后台任务
        </button>
        <button
          v-if="!readOnly && task.state === 'pending'"
          type="button"
          class="primary-link"
          :disabled="acting || unresolved"
          @click="act(task, true)"
        >
          确认参数并执行
        </button>
        <button
          v-if="!readOnly && ['pending', 'queued'].includes(task.state)"
          type="button"
          class="secondary-link"
          :disabled="acting || unresolved"
          @click="act(task, false)"
        >
          撤销未执行任务
        </button>
        <button
          v-if="
            task.run?.status === 'succeeded' &&
            task.workflow?.proposal.models?.some((m) => m.model === 'scat-gbar-v23')
          "
          type="button"
          class="secondary-link"
          :disabled="previewLoading"
          @click="showResult(task)"
        >
          查看已保存模型输出
        </button>
        <template v-for="(artifact, index) in task.run?.result_artifact_ids ?? []" :key="artifact">
          <button
            type="button"
            class="secondary-link"
            :disabled="previewLoading"
            @click="showResult(task, artifact)"
          >
            {{
              task.workflow?.proposal.kind === 'forecast'
                ? index === 0
                  ? '查看预测摘要'
                  : `查看预测轨迹 ${index}`
                : '查看分析结果'
            }}
          </button>
          <button type="button" class="secondary-link" @click="download(task, artifact)">
            下载完整结果 JSON
          </button>
        </template>
      </div>
      <AnalysisResultRenderer
        v-if="preview?.taskId === task.task_id"
        :result="preview.result"
        :artifact-id="preview.artifactId"
        :payload-sha256="preview.digest"
      />
    </article>
    <p v-if="previewLoading" role="status">正在读取结果…</p>
    <p v-if="previewError" role="alert">{{ previewError }}</p>
  </section>
</template>

<style scoped>
.model-task-panel {
  padding: 20px;
  margin-top: 20px;
}
.task-heading,
.task-buttons {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.task-buttons {
  justify-content: flex-start;
  margin-top: 16px;
}
.task-card {
  border-top: 1px solid var(--line);
  margin-top: 16px;
  padding-top: 12px;
  overflow-wrap: anywhere;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 360px;
  overflow-y: auto;
  font-size: 12px;
}
.task-id,
small,
.model-task-panel > p {
  color: var(--text-muted);
}
.tool-groups {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.tool-groups span {
  padding: 5px 10px;
  border: 1px solid var(--line);
  border-radius: 12px;
}
</style>
