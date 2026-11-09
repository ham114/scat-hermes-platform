<script setup lang="ts">
import { skillHandoff } from '@/lib/skillHandoff'
import { computed, inject, nextTick, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import {
  chatDisplayKey,
  createChatDisplay,
  displayIntent,
  isDisplayRequest,
} from '@/lib/chatDisplay'
import { routeLocationKey, routerKey } from 'vue-router'
import WorkspaceIcon from '@/components/WorkspaceIcon.vue'
import RunEvidencePanel from '@/components/RunEvidencePanel.vue'
import { isPlotRequest } from '@/lib/plotIntent'
import { conversationStorageKey } from '@/lib/portalSession'
import { loadConversationPage, validThread, type HistoryTurn } from '@/api/workspace'
import ModelTaskPanel from '@/components/ModelTaskPanel.vue'
import WorkflowCatalogPanel from '@/components/WorkflowCatalogPanel.vue'

import {
  createHermesRun,
  createHermesThread,
  getHermesRun,
  HermesApiError,
  newRequestId,
  readHermesEvents,
  stopHermesRun,
  prepareWorkflow,
} from '@/api/hermes'
import type { HermesEvent, HermesRun } from '@/api/hermes'

interface ToolStep {
  id: string
  tool: string
  label: string
  state: string
}
interface Message {
  runId?: string
  steps?: ToolStep[]
  role: 'user' | 'assistant'
  text: string
  status?: string
}
interface Pending {
  requestId: string
  message: string
  runId?: string
  sequence: number
}
interface Conversation {
  historyCursor?: string | null
  historical?: boolean
  threadId?: string
  messages: Message[]
  pending?: Pending
}
const storageKey = conversationStorageKey()
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
function restore(): Conversation {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null') as Conversation | null
    if (!value || (value.threadId && !uuid.test(value.threadId)) || !Array.isArray(value.messages))
      return { messages: [] }
    if (
      !value.messages.every(
        (item) =>
          item && ['user', 'assistant'].includes(item.role) && typeof item.text === 'string',
      )
    )
      return { messages: [] }
    if (
      value.pending &&
      (!uuid.test(value.pending.requestId) ||
        typeof value.pending.message !== 'string' ||
        !Number.isSafeInteger(value.pending.sequence) ||
        value.pending.sequence < -1 ||
        (value.pending.runId && !/^run_[A-Za-z0-9_-]{1,128}$/.test(value.pending.runId)))
    )
      return { messages: [] }
    if (value.pending?.runId && !value.threadId) return { messages: [] }
    return value
  } catch {
    return { messages: [] }
  }
}
const conversation = ref<Conversation>(restore())
const chatDisplay = createChatDisplay()
provide(chatDisplayKey, chatDisplay)
const input = ref('')
const readOnlyPreview = import.meta.env.VITE_WORKSPACE_READONLY === '1'
const busy = ref(false)
const stopping = ref(false)
const error = ref('')
const showWorkflows = ref(false)
const showTasks = ref(false)
const historical = ref(Boolean(conversation.value.historical))
const olderCursor = ref<string | null>(conversation.value.historyCursor ?? null)
const historyLoading = ref(false)
const textarea = ref<HTMLTextAreaElement>()
const messageEnd = ref<HTMLElement>()
const route = inject(routeLocationKey, undefined)
const router = inject(routerKey, undefined)
const contextBlocked = ref(false)
let alive = true
let historyGeneration = 0
let historyController: AbortController | undefined
const taskRevision = ref(0)
const pendingWorkflow = ref<{
  threadId: string
  requestId: string
  proposal: Record<string, unknown>
  budget: Record<string, number>
}>()
async function prepareDirect(proposal: Record<string, unknown>, budget: Record<string, number>) {
  if (
    readOnlyPreview ||
    busy.value ||
    conversation.value.pending ||
    historyLoading.value ||
    contextBlocked.value
  )
    return
  busy.value = true
  error.value = ''
  try {
    if (!conversation.value.threadId) {
      conversation.value.threadId = (await createHermesThread()).thread_id
      save()
    }
    const threadId = conversation.value.threadId
    if (pendingWorkflow.value && pendingWorkflow.value.threadId !== threadId)
      throw new Error('Original thread required')
    pendingWorkflow.value ??= { threadId, requestId: newRequestId(), proposal, budget }
    const request = pendingWorkflow.value
    await prepareWorkflow(request.threadId, request.proposal, {
      requestId: request.requestId,
      budget: request.budget,
    })
    pendingWorkflow.value = undefined
    taskRevision.value++
    showTasks.value = true
  } catch (cause) {
    if (cause instanceof HermesApiError && !cause.outcomeUnknown) pendingWorkflow.value = undefined
    error.value = pendingWorkflow.value
      ? '方案准备结果尚未确认，请先核对会话任务，再恢复原方案。不会自动确认或执行。'
      : '方案未被接受，请核对数据范围和模型条件后修改参数重新准备。'
    taskRevision.value++
    showTasks.value = true
  } finally {
    busy.value = false
  }
}
let controller: AbortController | undefined
const toolLabels: Record<string, string> = {
  skill_script_run: '执行技能脚本',
  skill_view: '读取技能',
  skills_list: '选择技能',
  scat_workflow_prepare: '准备方案',
  mcp__scat__search_scat_flights: '航班检索',
  mcp__scat__get_scat_batch_overview: '批次概览',
  mcp__scat__get_scat_track_points: '轨迹查询',
  mcp__scat__inspect_scat_collection_data_quality: '数据质量',
  mcp__scat__search_scat_knowledge: '文档检索',
}
const examples = [
  { title: '检索文档', icon: 'book', prompt: '解释 ADE 和 FDE 的含义、单位，并给出文档出处。' },
  {
    title: '比较方案',
    icon: 'flow',
    prompt: '我想比较不同方法的效果，请先询问数据范围和评估目标，再准备方案。',
  },
  {
    title: '整理报告',
    icon: 'file',
    prompt: '请根据当前会话中已有的分析结果整理报告，缺少结果时先告诉我需要补充什么。',
  },
]
const canSend = computed(
  () =>
    !readOnlyPreview &&
    Boolean(input.value.trim()) &&
    !busy.value &&
    !historyLoading.value &&
    !contextBlocked.value &&
    !conversation.value.pending,
)
function save() {
  if (!alive) return
  try {
    sessionStorage.setItem(storageKey, JSON.stringify(conversation.value))
  } catch {
    /* Browser storage may be unavailable. */
  }
}
function assistant(): Message {
  const last = conversation.value.messages.at(-1)
  if (last?.role === 'assistant') {
    last.runId = conversation.value.pending?.runId ?? last.runId
    return last
  }
  const value: Message = {
    role: 'assistant',
    text: '',
    status: '处理中',
    runId: conversation.value.pending?.runId,
  }
  conversation.value.messages.push(value)
  return conversation.value.messages.at(-1)!
}
function finish(state: HermesRun) {
  const message = assistant()
  if (state.status === 'completed') {
    if (state.completed === false || state.partial || state.interrupted)
      throw new HermesApiError('运行尚未完整结束，请查询状态。')
    if (typeof state.output === 'string') message.text = state.output
    message.status = '已完成'
  } else
    message.status =
      (
        { cancelled: '对话已停止', interrupted: '对话已中断', failed: '执行失败' } as Record<
          string,
          string
        >
      )[state.status] ?? state.status
  conversation.value.pending = undefined
  save()
}
function handle(event: HermesEvent) {
  if (!alive) return
  const pending = conversation.value.pending
  if (!pending) return
  if (event.sequence !== undefined) pending.sequence = event.sequence
  if (event.event === 'message.delta' && typeof event.delta === 'string')
    assistant().text += event.delta
  if (['tool.started', 'tool.completed'].includes(event.event) && event.tool) {
    const message = assistant()
    const steps = message.steps ?? (message.steps = [])
    let step = event.tool_call_id
      ? steps.find((x) => x.id === event.tool_call_id)
      : event.event === 'tool.completed'
        ? [...steps].reverse().find((x) => x.tool === event.tool && x.state === '执行中')
        : undefined
    if (!step) {
      step = {
        id: event.tool_call_id ?? event.tool + ':' + String(event.sequence ?? steps.length),
        tool: event.tool,
        label: toolLabels[event.tool] ?? '执行工具',
        state: '执行中',
      }
      steps.push(step)
    }
    if (event.event === 'tool.completed') step.state = event.error ? '失败' : '完成'
  }
  if (['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.event)) {
    finish({ ...event, status: event.event.slice(4) })
  }
  if (event.event === 'tool.completed') taskRevision.value++
  save()
}
async function resume() {
  if (busy.value || !conversation.value.pending) return
  if (readOnlyPreview && !conversation.value.pending.runId) return
  busy.value = true
  error.value = ''
  controller = new AbortController()
  const pending = conversation.value.pending
  try {
    if (!conversation.value.threadId) {
      conversation.value.threadId = (await createHermesThread(controller.signal)).thread_id
      save()
    }
    const tid = conversation.value.threadId
    if (!pending.runId) {
      const state = await createHermesRun(
        tid,
        pending.requestId,
        pending.message,
        controller.signal,
      )
      pending.runId = state.run_id
      save()
      if (['completed', 'failed', 'cancelled', 'interrupted'].includes(state.status)) {
        finish(state)
        return
      }
    }
    const state = await getHermesRun(tid, pending.runId, controller.signal)
    if (['completed', 'failed', 'cancelled', 'interrupted'].includes(state.status)) {
      finish(state)
      return
    }
    await readHermesEvents(tid, pending.runId, handle, controller.signal, pending.sequence)
  } catch (cause) {
    if (!controller.signal.aborted)
      error.value = cause instanceof HermesApiError ? cause.message : '连接中断，请恢复当前运行。'
  } finally {
    busy.value = false
    stopping.value = false
    save()
  }
}
async function send() {
  if (!canSend.value) return
  const text = input.value.trim()
  const command = displayIntent(text)
  if (command || isDisplayRequest(text)) {
    input.value = ''
    try {
      if (!command)
        throw Error(
          '当前支持“将当前结果改成双列”“把图表改成柱状图”“隐藏第2项”等展示调整；请与重新分析分开发送。',
        )
      const message = chatDisplay.apply(command)
      conversation.value.messages.push(
        { role: 'user', text },
        { role: 'assistant', text: message, status: '展示调整' },
      )
      error.value = ''
      save()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '展示调整失败'
    }
    return
  }
  conversation.value.messages.push({ role: 'user', text })
  conversation.value.pending = { requestId: newRequestId(), message: text, sequence: -1 }
  input.value = ''
  showTasks.value = false
  save()
  await resume()
}
async function stop() {
  if (readOnlyPreview) return
  const pending = conversation.value.pending
  if (!pending?.runId || !conversation.value.threadId || stopping.value) return
  stopping.value = true
  try {
    const state = await stopHermesRun(conversation.value.threadId, pending.runId)
    if (['completed', 'failed', 'cancelled', 'interrupted'].includes(state.status)) {
      finish(state)
      controller?.abort()
      busy.value = false
    } else if (!busy.value) await resume()
  } catch {
    error.value = '停止请求未确认，请查询运行状态。'
    stopping.value = false
  }
}
function startNew() {
  if (busy.value || conversation.value.pending) return
  historyGeneration++
  chatDisplay.clear()
  pendingWorkflow.value = undefined
  historyController?.abort()
  historyLoading.value = false
  conversation.value = { messages: [] }
  historical.value = false
  olderCursor.value = null
  input.value = ''
  contextBlocked.value = false
  if (route?.query.thread_id) void router?.replace({ path: route.path, query: {} })
  showTasks.value = false
  error.value = ''
  save()
}

function historyMessages(rows: HistoryTurn[]): Message[] {
  return rows.flatMap((row) => [
    { role: 'user' as const, text: row.input ?? '提问原文未保存' },
    {
      role: 'assistant' as const,
      text: row.output ?? '回复未保存或暂不可用',
      runId: row.run_id ?? undefined,
      status:
        (
          {
            completed: '已完成',
            failed: '执行失败',
            cancelled: '对话已停止',
            interrupted: '对话已中断',
            unavailable: '旧记录',
            admitting: '准入待确认',
            running: '处理中',
            started: '处理中',
            queued: '处理中',
          } as Record<string, string>
        )[row.status] ?? row.status,
    },
  ])
}
async function olderHistory() {
  const tid = conversation.value.threadId
  if (!tid || !olderCursor.value || historyLoading.value || busy.value) return
  const token = ++historyGeneration
  chatDisplay.clear()
  historyController?.abort()
  historyController = new AbortController()
  historyLoading.value = true
  try {
    const page = await loadConversationPage(tid, olderCursor.value, historyController.signal)
    if (token !== historyGeneration) return
    conversation.value.messages = [...historyMessages(page.items), ...conversation.value.messages]
    olderCursor.value = page.next_cursor
    conversation.value.historyCursor = page.next_cursor
    save()
  } catch {
    if (token === historyGeneration) error.value = '更早记录暂时不可用。'
  } finally {
    if (token === historyGeneration) historyLoading.value = false
  }
}

async function loadThread(value: unknown) {
  if (!validThread(value)) {
    contextBlocked.value = true
    error.value = '会话编号无效。'
    return
  }
  if (value === conversation.value.threadId) {
    if (conversation.value.pending?.runId) void resume()
    if (conversation.value.pending) return
  }
  if (busy.value || conversation.value.pending) {
    contextBlocked.value = true
    error.value = '请先恢复或停止当前运行，再切换会话。'
    return
  }
  const token = ++historyGeneration
  historyController?.abort()
  historyController = new AbortController()
  historyLoading.value = true
  error.value = ''
  contextBlocked.value = true
  try {
    const page = await loadConversationPage(value, undefined, historyController.signal)
    if (token !== historyGeneration) return
    const rows = page.items
    olderCursor.value = page.next_cursor
    conversation.value = {
      threadId: value,
      messages: historyMessages(rows),
      historyCursor: page.next_cursor,
      historical: true,
    }
    historical.value = true
    contextBlocked.value = false
    const active = [...rows]
      .reverse()
      .find((row) => ['admitting', 'started', 'queued', 'running'].includes(row.status))
    if (active)
      conversation.value.pending = {
        requestId: active.request_id,
        message: active.input ?? '',
        runId: active.run_id ?? undefined,
        sequence: active.last_sequence,
      }
    save()
    if (active?.run_id) void resume()
  } catch {
    if (token === historyGeneration) error.value = '历史回复暂时不可用，请重试读取。'
  } finally {
    if (token === historyGeneration) historyLoading.value = false
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.isComposing && !event.shiftKey && !event.altKey) {
    event.preventDefault()
    void send()
  }
}
function fillExample(value: string) {
  input.value = value
  void nextTick(() => textarea.value?.focus())
}
watch(
  () => route?.query.thread_id,
  (value) => {
    if (value) void loadThread(value)
  },
)
watch(
  () => conversation.value.messages.at(-1)?.text,
  () => {
    const follow =
      window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 180
    if (follow) void nextTick(() => messageEnd.value?.scrollIntoView?.({ block: 'nearest' }))
  },
)
onMounted(() => {
  if (skillHandoff.value) {
    input.value = skillHandoff.value
    skillHandoff.value = ''
  }
  if (route?.query.thread_id) void loadThread(route.query.thread_id)
  else if (conversation.value.pending?.runId) void resume()
})
onUnmounted(() => {
  historyGeneration++
  historyController?.abort()
  save()
  alive = false
  controller?.abort()
})
</script>

<template>
  <section
    class="assistant-page"
    :class="{ 'assistant-page-empty': !conversation.messages.length && !historyLoading }"
  >
    <p v-if="readOnlyPreview" class="composer-footnote">只读预览</p>
    <div
      v-if="conversation.threadId || conversation.messages.length || contextBlocked"
      class="assistant-session-bar"
    >
      <span>{{ historical ? '历史回复' : '当前对话' }}</span>
      <button
        type="button"
        class="workspace-new-chat"
        :disabled="busy || Boolean(conversation.pending)"
        @click="startNew"
      >
        <WorkspaceIcon name="plus" :size="16" />新建会话
      </button>
    </div>
    <header v-if="!conversation.messages.length && !historyLoading" class="assistant-welcome">
      <span class="assistant-welcome-mark"><WorkspaceIcon name="spark" :size="30" /></span>
      <h1>有什么需要一起完成？</h1>
    </header>
    <p v-if="historyLoading" class="history-placeholder" role="status">正在读取历史回复…</p>
    <button
      v-if="olderCursor"
      class="secondary-link"
      :disabled="historyLoading || busy"
      @click="olderHistory"
    >
      加载更早记录
    </button>
    <div class="assistant-messages" role="log" aria-live="polite" aria-label="对话记录">
      <article
        v-for="(message, index) in conversation.messages"
        :key="index"
        class="assistant-message"
        :class="message.role"
        :data-run-id="message.runId"
      >
        <span class="message-avatar" aria-hidden="true"
          ><WorkspaceIcon v-if="message.role === 'assistant'" name="spark" :size="18" /><template
            v-else
            >我</template
          ></span
        >
        <div class="message-content">
          <strong class="message-role">{{ message.role === 'user' ? '你' : 'Hermes' }}</strong>
          <p>{{ message.text || (message.status === '处理中' ? '正在整理结果…' : '') }}</p>
          <details v-if="message.steps?.length" class="message-process">
            <summary>执行过程 · {{ message.steps.length }} 步</summary>
            <ol>
              <li v-for="step in message.steps" :key="step.id">
                <span
                  class="step-indicator"
                  :class="{ complete: step.state === '完成', failed: step.state === '失败' }"
                />{{ step.label }} · {{ step.state }}
              </li>
            </ol>
          </details>
          <div v-if="message.role === 'assistant'" class="message-footer">
            <small v-if="message.status">{{ message.status }}</small>
            <RunEvidencePanel
              v-if="conversation.threadId && message.runId && message.status !== '处理中'"
              :status="message.status"
              :auto-plot="
                index === conversation.messages.length - 1 &&
                isPlotRequest(conversation.messages[index - 1]?.text ?? '')
              "
              :thread-id="conversation.threadId"
              :run-id="message.runId"
            />
          </div>
        </div>
      </article>
    </div>
    <ModelTaskPanel
      :read-only="readOnlyPreview"
      :thread-id="conversation.threadId"
      :revision="taskRevision"
      :show-empty="showTasks"
    />
    <div ref="messageEnd" />
    <p v-if="error" class="assistant-error" role="alert">{{ error }}</p>
    <div
      v-if="conversation.pending && !busy && (!readOnlyPreview || conversation.pending.runId)"
      class="assistant-recovery"
    >
      <button type="button" class="secondary-link" @click="resume">恢复本次请求</button>
    </div>
    <form class="assistant-composer" @submit.prevent="send">
      <label class="sr-only" for="assistant-input">你的问题</label>
      <textarea
        id="assistant-input"
        ref="textarea"
        v-model="input"
        rows="3"
        maxlength="32000"
        :disabled="
          busy ||
          Boolean(conversation.pending) ||
          historyLoading ||
          contextBlocked ||
          readOnlyPreview
        "
        placeholder="描述你的问题，或想完成的事情…"
        @keydown="keydown"
      />
      <div class="assistant-actions">
        <div class="composer-options">
          <button
            type="button"
            class="composer-option"
            :aria-expanded="showWorkflows"
            @click="showWorkflows = !showWorkflows"
          >
            <WorkspaceIcon name="flow" :size="17" />流程模板
          </button>
          <button
            v-if="conversation.threadId"
            type="button"
            class="composer-option"
            :aria-expanded="showTasks"
            @click="showTasks = !showTasks"
          >
            <WorkspaceIcon name="file" :size="17" />会话任务
          </button>
        </div>
        <span v-if="busy" class="composer-processing" role="status">{{
          stopping ? '正在停止…' : '正在处理…'
        }}</span>
        <button
          v-if="conversation.pending?.runId && !readOnlyPreview"
          type="button"
          class="secondary-link"
          :disabled="stopping"
          @click="stop"
        >
          停止
        </button>
        <button type="submit" class="composer-send" aria-label="发送问题" :disabled="!canSend">
          <WorkspaceIcon name="arrow" :size="19" /><span>发送</span>
        </button>
      </div>
    </form>
    <div v-if="!conversation.messages.length && !historyLoading" class="assistant-suggestions">
      <button
        v-for="example in examples"
        :key="example.title"
        type="button"
        @click="fillExample(example.prompt)"
      >
        <WorkspaceIcon :name="example.icon" :size="16" />{{ example.title }}
      </button>
    </div>
    <p
      v-if="busy || conversation.messages.at(-1)?.status === '对话已停止'"
      class="composer-footnote"
    >
      停止对话不会取消已提交的业务任务。
    </p>
    <WorkflowCatalogPanel
      v-if="showWorkflows"
      :disabled="
        busy ||
        Boolean(pendingWorkflow) ||
        Boolean(conversation.pending) ||
        historyLoading ||
        contextBlocked
      "
      :read-only="readOnlyPreview"
      @example="fillExample($event)"
      @prepare="prepareDirect"
    />
    <button
      v-if="pendingWorkflow && !busy && !readOnlyPreview"
      type="button"
      class="secondary-link"
      @click="prepareDirect(pendingWorkflow.proposal, pendingWorkflow.budget)"
    >
      核对并恢复原方案
    </button>
  </section>
</template>
