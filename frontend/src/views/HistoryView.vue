<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import {
  listConversationThreads,
  loadConversationPage,
  renameHistoryThread,
  type HistoryThread,
  type HistoryTurn,
} from '@/api/workspace'
import WorkspaceIcon from '@/components/WorkspaceIcon.vue'
const threads = ref<HistoryThread[]>([]),
  turns = ref<HistoryTurn[]>([])
const selected = ref(''),
  threadCursor = ref<string | null>(null),
  turnCursor = ref<string | null>(null)
const loading = ref(false),
  reading = ref(false),
  error = ref(''),
  editing = ref(false),
  title = ref(''),
  saving = ref(false)
const readonly = import.meta.env.VITE_WORKSPACE_READONLY === '1'
let generation = 0,
  listGeneration = 0,
  alive = true
let controller: AbortController | undefined
const date = (time: number) =>
  new Date(time * 1000).toLocaleString('zh-CN', {
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
const labels: Record<string, string> = {
  completed: '已完成',
  failed: '执行失败',
  cancelled: '已停止',
  interrupted: '已中断',
  unavailable: '旧记录',
  admitting: '准入待确认',
  running: '运行中',
  queued: '排队中',
  started: '运行中',
}
async function refresh(more = false) {
  if (loading.value) return
  const token = ++listGeneration
  loading.value = true
  error.value = ''
  try {
    const page = await listConversationThreads(more ? (threadCursor.value ?? undefined) : undefined)
    if (!alive || token !== listGeneration) return
    threads.value = more
      ? [
          ...threads.value,
          ...page.items.filter((x) => !threads.value.some((y) => y.thread_id === x.thread_id)),
        ]
      : page.items
    threadCursor.value = page.next_cursor
  } catch {
    if (alive && token === listGeneration) error.value = '历史会话暂时不可用。'
  } finally {
    if (alive && token === listGeneration) loading.value = false
  }
}
async function select(thread: string, more = false) {
  if (more && reading.value) return
  const token = ++generation
  controller?.abort()
  controller = new AbortController()
  if (!more) {
    selected.value = thread
    turns.value = []
    turnCursor.value = null
    editing.value = false
  }
  reading.value = true
  error.value = ''
  try {
    const page = await loadConversationPage(
      thread,
      more ? (turnCursor.value ?? undefined) : undefined,
      controller.signal,
    )
    if (!alive || token !== generation) return
    turns.value = more
      ? [...page.items.filter((x) => !turns.value.some((y) => y.id === x.id)), ...turns.value]
      : page.items
    turnCursor.value = page.next_cursor
  } catch {
    if (alive && token === generation) error.value = '会话暂时不可用，请重试读取。'
  } finally {
    if (alive && token === generation) reading.value = false
  }
}
function edit() {
  title.value = threads.value.find((x) => x.thread_id === selected.value)?.title ?? ''
  editing.value = true
}
async function rename() {
  if (readonly || saving.value || !title.value.trim()) return
  const thread = threads.value.find((x) => x.thread_id === selected.value)
  if (!thread) return
  const token = generation
  saving.value = true
  error.value = ''
  try {
    const updated = await renameHistoryThread(
      thread.thread_id,
      title.value,
      thread.title_revision ?? 0,
    )
    if (!alive || token !== generation) return
    Object.assign(thread, updated)
    editing.value = false
  } catch {
    if (alive && token === generation) error.value = '标题保存未确认，请刷新列表核对后重试。'
  } finally {
    if (alive) saving.value = false
  }
}
onMounted(() => refresh())
onUnmounted(() => {
  alive = false
  generation++
  listGeneration++
  controller?.abort()
})
</script>
<template>
  <section class="workspace-page history-page">
    <header class="workspace-page-heading">
      <div>
        <span class="workspace-kicker">记录</span>
        <h1>历史会话</h1>
      </div>
      <button class="secondary-link" :disabled="loading" @click="refresh()">刷新</button>
    </header>
    <p v-if="error" class="assistant-error" role="alert">{{ error }}</p>
    <div v-if="!loading && !threads.length && !error" class="workspace-empty">
      <WorkspaceIcon name="clock" :size="32" />
      <h2>还没有会话</h2>
      <router-link class="secondary-link" to="/">开始对话</router-link>
    </div>
    <div class="history-layout">
      <nav class="history-list" aria-label="历史会话列表">
        <button
          v-for="thread in threads"
          :key="thread.thread_id"
          class="history-item"
          :class="{ selected: selected === thread.thread_id }"
          :aria-pressed="selected === thread.thread_id"
          @click="select(thread.thread_id)"
        >
          <WorkspaceIcon name="chat" :size="18" /><span
            ><strong>{{ thread.title || '会话 · ' + date(thread.created_at) }}</strong
            ><small>{{ date(thread.created_at) }}</small></span
          ><WorkspaceIcon name="chevron" :size="16" />
        </button>
        <button
          v-if="threadCursor"
          class="secondary-link"
          :disabled="loading"
          @click="refresh(true)"
        >
          更多会话
        </button>
        <p v-if="loading" role="status">正在读取…</p>
      </nav>
      <section class="history-detail" aria-label="完整会话记录">
        <p v-if="!selected" class="history-placeholder">选择一个会话</p>
        <template v-else>
          <div class="history-detail-heading">
            <h2>{{ threads.find((x) => x.thread_id === selected)?.title || '会话记录' }}</h2>
            <div class="task-buttons">
              <button v-if="!readonly" class="secondary-link" :disabled="saving" @click="edit">
                修改标题</button
              ><router-link
                v-if="!reading && !error"
                class="primary-link"
                :to="{ path: '/assistant', query: { thread_id: selected } }"
                >继续对话</router-link
              >
            </div>
          </div>
          <form v-if="editing" class="result-entry-form" @submit.prevent="rename">
            <label class="sr-only" for="history-title">会话标题</label
            ><input id="history-title" v-model="title" maxlength="100" :disabled="saving" /><button
              class="primary-link"
              :disabled="saving || !title.trim()"
            >
              保存标题</button
            ><button type="button" class="secondary-link" @click="editing = false">取消</button>
          </form>
          <button
            v-if="turnCursor"
            class="secondary-link"
            :disabled="reading"
            @click="select(selected, true)"
          >
            加载更早记录
          </button>
          <p v-if="reading" role="status">正在读取…</p>
          <p v-else-if="!turns.length && !error" class="history-placeholder">暂无消息</p>
          <article v-for="turn in turns" :key="turn.id" class="history-reply">
            <small>你</small>
            <p :class="{ 'history-placeholder': !turn.input_available }">
              {{ turn.input_available ? turn.input : '提问原文未保存' }}
            </p>
            <small>Hermes · {{ labels[turn.status] ?? turn.status }}</small>
            <p>{{ turn.output ?? '回复未保存或暂不可用' }}</p>
          </article>
        </template>
      </section>
    </div>
  </section>
</template>
<style scoped>
.history-item strong {
  overflow-wrap: anywhere;
  white-space: normal;
}
.history-detail-heading {
  flex-wrap: wrap;
}
.history-detail-heading h2 {
  overflow-wrap: anywhere;
}
.result-entry-form {
  margin: 16px 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.result-entry-form input {
  min-width: 0;
  flex: 1;
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: 10px;
  color: var(--text);
  background: var(--surface);
}
</style>
