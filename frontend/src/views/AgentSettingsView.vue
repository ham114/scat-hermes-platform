<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { authorRequest } from '@/api/skills'
import { newRequestId } from '@/api/hermes'
interface Section {
  section: string
  value: Record<string, unknown>
  config_sha256: string
  runtime_observed: boolean
}
interface Snapshot {
  sections: Section[]
  profiles: {
    name: string
    role: string
    key_configured: boolean
    directories_valid: boolean
    state_db_exists: boolean
  }[]
  proposals: { id: string; section: string; state: string; payload: string }[]
  usage: { day: string; today: Record<string, number>; lifetime: Record<string, number> }
  optional_groups: Record<string, string[]>
}
const key = ref(''),
  releaseKey = ref(''),
  snapshot = ref<Snapshot>(),
  selected = ref('scat-main'),
  editor = ref('{}'),
  busy = ref(false),
  error = ref(''),
  preview = ref<unknown>(),
  confirmed = ref(false),
  request = ref<{ section: string; changes: object; expected_sha256: string; request_id: string }>()
const readonly = import.meta.env.VITE_SCAT_READONLY_PREVIEW === '1'
let alive = true
const labels: Record<string, string> = {
  'scat-main': '主对话',
  'scat-worker': '后台执行',
  'scat-author': '技能作者',
  memory: '记忆参数',
  learning: '自动学习参数',
  proxy: '每日与累计预算',
  policy: '运行预算',
  tools: '工具组限制',
}
const section = computed(() => snapshot.value?.sections.find((x) => x.section === selected.value))
async function api<T>(path: string, method = 'GET', body?: unknown, release = false) {
  return authorRequest<T>(
    path,
    key.value,
    method,
    body,
    release ? releaseKey.value : undefined,
    '/author-api/v1/settings',
  )
}
async function action(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await fn()
  } catch (e) {
    if (alive) error.value = e instanceof Error ? e.message : '操作失败'
  } finally {
    busy.value = false
    releaseKey.value = ''
  }
}
function invalidate() {
  preview.value = undefined
  request.value = undefined
  confirmed.value = false
}
function select(value: string) {
  selected.value = value
  invalidate()
  const s = snapshot.value?.sections.find((x) => x.section === value)
  editor.value = JSON.stringify(
    value.startsWith('scat-')
      ? {
          model: (s?.value.model as { default: string })?.default,
          temperature: (s?.value.agent as { temperature: number })?.temperature,
          max_turns: (s?.value.agent as { max_turns: number })?.max_turns,
        }
      : (s?.value ?? {}),
    null,
    2,
  )
}
async function refresh() {
  const s = await api<Snapshot>('')
  if (alive) {
    snapshot.value = s
    select(selected.value)
  }
}
async function inspect() {
  if (!section.value) return
  invalidate()
  request.value = {
    section: selected.value,
    changes: JSON.parse(editor.value),
    expected_sha256: section.value.config_sha256,
    request_id: newRequestId(),
  }
  preview.value = await api('/preview', 'POST', request.value)
}
async function stage() {
  if (!preview.value || !request.value || !confirmed.value) return
  await api('/proposals', 'POST', { ...request.value, confirmed: true }, true)
  invalidate()
  await refresh()
}
onUnmounted(() => {
  alive = false
  key.value = ''
  releaseKey.value = ''
})
</script>
<template>
  <section class="settings-page" :aria-busy="busy">
    <h1>Agent 设置</h1>
    <form @submit.prevent="action(refresh)">
      <input
        v-model="key"
        type="password"
        aria-label="设置管理凭据"
        placeholder="管理凭据"
        autocomplete="off"
        :disabled="busy"
      /><button :disabled="busy">连接 / 刷新配置</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="snapshot"
      ><p role="status">
        以下为注册目标的磁盘配置，运行中实际配置尚未核实。保存修改不代表服务已生效。
      </p>
      <div class="profiles">
        <article v-for="p in snapshot.profiles" :key="p.name">
          <h2>{{ labels[p.name] }}</h2>
          <p>
            凭据{{ p.key_configured ? '已配置' : '未配置' }} · 目录{{
              p.directories_valid ? '正常' : '异常'
            }}
            · 状态库{{ p.state_db_exists ? '存在' : '缺失' }}
          </p>
        </article>
      </div>
      <section class="panel">
        <h2>模型额度</h2>
        <p>日期 {{ snapshot.usage.day }}（北京时间）</p>
        <div class="usage">
          <div
            v-for="(values, name) in { 今日: snapshot.usage.today, 累计: snapshot.usage.lifetime }"
            :key="name"
          >
            <h3>{{ name }}</h3>
            <dl>
              <template v-for="(v, k) in values" :key="k"
                ><dt>
                  {{
                    {
                      requests: '请求次数',
                      reserved_units: '保守计量单位',
                      input_tokens: '供应商输入 token',
                      output_tokens: '供应商输出 token',
                    }[k] ?? k
                  }}
                </dt>
                <dd>{{ v }}</dd></template
              >
            </dl>
          </div>
        </div>
        <p>失败与结果不明请求不退回预留。每日、累计和单次运行上限分别约束；不能以此换算费用。</p>
      </section>
      <section class="panel">
        <h2>配置</h2>
        <select
          :value="selected"
          aria-label="配置范围"
          :disabled="busy"
          @change="select(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="s in snapshot.sections" :key="s.section" :value="s.section">
            {{ labels[s.section] ?? s.section }}
          </option>
        </select>
        <details>
          <summary>当前磁盘配置</summary>
          <pre>{{ JSON.stringify(section?.value, null, 2) }}</pre>
        </details>
        <p v-if="selected === 'tools'">
          工具权限由角色固定。这里只能禁用列出的可选组，不能增加权限；空数组表示不额外禁用。
        </p>
        <pre v-if="selected === 'tools'">{{
          JSON.stringify(snapshot.optional_groups, null, 2)
        }}</pre>
        <p v-if="selected === 'proxy' || selected === 'policy'">
          此入口只允许收紧已有预算，不清零账本或移除累计封顶。
        </p>
        <textarea
          v-model="editor"
          aria-label="配置修改JSON"
          :disabled="busy || readonly"
          maxlength="32768"
          spellcheck="false"
          @input="invalidate"
        /><button :disabled="busy || readonly" @click="action(inspect)">校验并预览差异</button>
      </section>
      <section v-if="preview" class="panel">
        <h2>修改预览</h2>
        <pre>{{ JSON.stringify(preview, null, 2) }}</pre>
        <label
          ><input
            v-model="confirmed"
            type="checkbox"
            :disabled="busy"
          />确认保存为待维护应用，不立即重启服务</label
        ><input
          v-model="releaseKey"
          type="password"
          aria-label="独立配置确认凭据"
          placeholder="独立确认凭据"
          autocomplete="off"
          :disabled="busy"
        /><button :disabled="busy || readonly || !confirmed || !releaseKey" @click="action(stage)">
          保存待应用修改
        </button>
      </section>
      <section class="panel">
        <h2>配置变更记录</h2>
        <article v-for="p in snapshot.proposals" :key="p.id">
          <p>
            {{ labels[p.section] ?? p.section }} ·
            {{
              p.state === 'pending_maintenance'
                ? '等待维护应用'
                : p.state === 'written_restart_required'
                  ? '已写配置，需部署/重启核验'
                  : p.state
            }}
          </p>
          <code>{{ p.id }}</code>
          <details>
            <summary>修改内容</summary>
            <pre>{{ p.payload }}</pre>
          </details>
        </article>
        <p>
          维护流程须停用自有服务、确认无在途或未知任务后应用。网页不会执行Docker、安装依赖或修改密钥。
        </p>
      </section>
    </template>
  </section>
</template>
<style scoped>
.settings-page {
  max-width: 1100px;
  margin: auto;
  padding: 28px;
}
.panel {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 20px;
  margin: 20px 0;
}
.profiles,
.usage {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
}
article {
  min-width: 0;
  padding: 12px 0;
  border-bottom: 1px solid var(--line);
}
textarea {
  width: 100%;
  box-sizing: border-box;
  min-height: 210px;
  font-family: monospace;
}
input,
textarea,
select {
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
  padding: 10px;
  max-width: 100%;
}
button {
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface-raised);
  color: var(--text);
  margin: 5px;
}
button:disabled {
  opacity: 0.5;
}
pre,
p,
code {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
label {
  display: block;
  margin: 12px 0;
}
h2 {
  font-size: 18px;
}
@media (max-width: 600px) {
  .settings-page {
    padding: 16px;
  }
}
</style>
