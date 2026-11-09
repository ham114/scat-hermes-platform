<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import {
  memoryRequest,
  memorySummary,
  type MemoryStatus,
  type Correction,
  type LearningStatus,
} from '@/api/memory'
import { newRequestId } from '@/api/hermes'
const status = ref<MemoryStatus>(),
  records = ref<Correction[]>([]),
  learning = ref<LearningStatus>()
const context = ref(''),
  content = ref(''),
  error = ref(''),
  notice = ref(''),
  busy = ref(false),
  clearConfirm = ref(false),
  tab = ref('memory')
const pending = ref<{ request_id: string; content: string }>(),
  uncertainDelete = ref(false)
const readonly = import.meta.env.VITE_SCAT_READONLY_PREVIEW === '1'
let alive = true
const labels: Record<string, string> = {
  pending: '待处理',
  sending: '发送中',
  sent: '已提交记忆服务',
  unknown: '结果待核实',
  cancelled: '已取消',
}
async function action(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    if (alive) error.value = e instanceof Error ? e.message : '请求失败'
  } finally {
    busy.value = false
  }
}
async function refresh() {
  const [s, r] = await Promise.all([
    memoryRequest<MemoryStatus>(),
    memoryRequest<{ items: Correction[] }>('/corrections'),
  ])
  if (!alive) return
  if (status.value && status.value.epoch !== s.epoch) {
    pending.value = undefined
    content.value = ''
  }
  status.value = s
  records.value = r.items
  if (pending.value && r.items.some((x) => x.request_id === pending.value!.request_id)) {
    pending.value = undefined
    content.value = ''
  }
  if (!s.enabled) context.value = ''
  if (s.deletion === 'completed') notice.value = '长期记忆已确认清空，当前仍保持暂停'
  if (s.deletion === 'pending' || s.deletion === 'completed') uncertainDelete.value = false
}
async function recall() {
  const r = await memoryRequest<{ enabled: boolean; available?: boolean; context: string }>(
    '/context',
  )
  if (alive) {
    context.value = memorySummary(r.context)
    notice.value = !r.enabled
      ? '记忆已暂停'
      : r.available
        ? context.value
          ? '已读取当前摘要'
          : '尚无已形成摘要，写入可能待处理；不能据此判定已记住'
        : '记忆服务暂不可用'
  }
}
async function correct() {
  if (!pending.value) pending.value = { request_id: newRequestId(), content: content.value.trim() }
  await memoryRequest('/corrections', 'POST', pending.value)
  notice.value = '更正已提交，等待处理；不代表召回内容已经更新'
  await refresh()
}
async function clear() {
  if (!clearConfirm.value) return
  uncertainDelete.value = true
  clearConfirm.value = false
  context.value = ''
  await memoryRequest('', 'DELETE')
  notice.value = '删除已提交，等待后台确认'
  await refresh()
}
async function toggle() {
  await memoryRequest('/settings', 'PUT', { enabled: !status.value!.enabled })
  context.value = ''
  await refresh()
}
async function readLearning() {
  const r = await memoryRequest<LearningStatus>('/learning')
  if (alive) learning.value = r
}
function learningTab() {
  tab.value = 'learning'
  void action(readLearning)
}
onMounted(() => void action(refresh))
onUnmounted(() => {
  alive = false
  context.value = ''
  content.value = ''
  pending.value = undefined
  records.value = []
})
</script>
<template>
  <section class="memory-page" :aria-busy="busy">
    <h1>记忆</h1>
    <nav class="controls">
      <button @click="tab = 'memory'">我的记忆</button
      ><button @click="learningTab">自动学习</button>
    </nav>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="tab === 'memory'">
      <div class="controls">
        <span>{{ status ? (status.enabled ? '记忆已启用' : '记忆已暂停') : '状态未读取' }}</span
        ><button :disabled="busy" @click="action(refresh)">刷新状态</button
        ><button
          :disabled="
            busy || readonly || !status || status.deletion === 'pending' || uncertainDelete
          "
          @click="action(toggle)"
        >
          {{ status?.enabled ? '暂停记忆' : '启用记忆' }}
        </button>
      </div>
      <p v-if="status?.deletion !== 'none' && status">
        清空状态：{{ status.deletion === 'completed' ? '已确认完成' : '后台处理中' }}
      </p>
      <p v-if="uncertainDelete" role="status">删除请求待核实，请刷新状态；不会自动重复提交。</p>
      <p v-if="status">
        写入队列：{{
          Object.entries(status.queue)
            .map(([k, v]) => (labels[k] ?? k) + ' ' + v)
            .join(' · ') || '无待处理项'
        }}
      </p>
      <section class="panel">
        <h2>当前记忆摘要</h2>
        <button :disabled="busy || !status?.enabled" @click="action(recall)">
          查看 / 刷新摘要
        </button>
        <pre v-if="context">{{ context }}</pre>
        <p v-else>尚未读取可用摘要</p>
      </section>
      <section class="panel">
        <h2>更正记忆</h2>
        <textarea
          v-model="content"
          aria-label="更正内容"
          placeholder="例如：请更正，我希望使用公里，而非海里。"
          maxlength="2000"
          :disabled="busy || readonly || !!pending"
        />
        <button
          :disabled="busy || readonly || !status?.enabled || (!pending && !content.trim())"
          @click="action(correct)"
        >
          {{ pending ? '核对后重试原请求' : '提交更正' }}
        </button>
        <p>已提交只表示进入处理流程；实际效果请稍后刷新摘要核对。</p>
        <article v-for="item in records" :key="item.request_id">
          <p>{{ item.content }}</p>
          <small
            >{{ labels[item.state] ?? item.state }} ·
            {{ new Date(item.created * 1000).toLocaleString() }}</small
          >
        </article>
      </section>
      <section class="panel">
        <h2>清空长期记忆</h2>
        <p>清空当前用户的Honcho长期记忆，不删除聊天历史、技能草稿或备份。完成后保持暂停。</p>
        <label
          ><input
            v-model="clearConfirm"
            type="checkbox"
            :disabled="busy || readonly"
          />确认清空当前用户的长期记忆</label
        ><button
          :disabled="
            busy ||
            readonly ||
            !clearConfirm ||
            !status ||
            status.deletion === 'pending' ||
            uncertainDelete
          "
          @click="action(clear)"
        >
          清空记忆
        </button>
      </section>
    </template>
    <section v-else class="panel">
      <h2>自动学习</h2>
      <button :disabled="busy" @click="action(readLearning)">刷新学习状态</button>
      <template v-if="learning"
        ><p>
          {{ learning.enabled ? '采集与自动作者已配置启用' : '学习已配置停用' }} · 调度进程{{
            learning.scheduler_observed ? '已核实' : '未核实'
          }}
          · 自动发布关闭
        </p>
        <p>
          可信经验 {{ learning.verified_experiences }} · 自动试跑{{
            learning.auto_trials ? '启用' : '关闭'
          }}
        </p>
        <dl>
          <template v-for="(value, key) in learning.thresholds" :key="key"
            ><dt>
              {{
                {
                  min_successes: '最低成功次数',
                  min_distinct_inputs: '最低独立输入',
                  cooldown_hours: '冷却小时',
                  max_drafts_per_day: '每日草稿上限',
                }[key] ?? key
              }}
            </dt>
            <dd>{{ value }}</dd></template
          >
        </dl>
        <article v-for="p in learning.patterns" :key="p.pattern">
          <code>{{ p.pattern }}</code>
          <p>
            成功 {{ p.successes }} / {{ learning.thresholds.min_successes }} · 独立输入
            {{ p.distinct_inputs }} / {{ learning.thresholds.min_distinct_inputs }}
          </p>
        </article>
        <article v-for="job in learning.jobs" :key="job.id">
          <p>{{ job.id }} · {{ job.state }}</p>
          <p v-if="job.error">{{ job.error }}</p>
          <router-link
            v-if="job.draft"
            :to="{ path: '/skills', query: { draft: job.draft, trial: job.trial } }"
            >查看草稿与试跑</router-link
          >
          <p v-if="job.state === 'unknown'">结果待核实，不自动重放；需维护者排查</p>
        </article>
      </template>
    </section>
  </section>
</template>
<style scoped>
.memory-page {
  max-width: 1040px;
  margin: auto;
  padding: 28px;
}
.panel {
  border: 1px solid var(--line);
  padding: 20px;
  border-radius: 12px;
  margin: 20px 0;
}
.controls {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
button {
  padding: 9px 12px;
  border: 1px solid var(--line);
  background: var(--surface-raised);
  color: var(--text);
  border-radius: 6px;
  margin: 4px;
}
button:disabled {
  opacity: 0.5;
}
textarea {
  box-sizing: border-box;
  width: 100%;
  min-height: 100px;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  padding: 12px;
}
pre,
code,
p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
article {
  border-top: 1px solid var(--line);
  padding: 12px 0;
}
h2 {
  font-size: 18px;
}
label {
  display: block;
  margin: 12px 0;
}
@media (max-width: 600px) {
  .memory-page {
    padding: 16px;
  }
}
</style>
