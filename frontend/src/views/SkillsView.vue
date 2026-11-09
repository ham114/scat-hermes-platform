<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  authorRequest,
  type SkillEntry,
  type Draft,
  type Trial,
  type ReleasePreview,
} from '@/api/skills'
import { skillHandoff } from '@/lib/skillHandoff'
const router = useRouter()
const route = useRoute()
const key = ref(''),
  releaseKey = ref(''),
  busy = ref(false),
  error = ref('')
const readonly = import.meta.env.VITE_SCAT_READONLY_PREVIEW === '1'
const skills = ref<SkillEntry[]>([]),
  bundle = ref('')
const drafts = ref<{ id: string; head: number }[]>([])
const selected = ref<SkillEntry>(),
  draft = ref<Draft>(),
  editor = ref('{"changes": []}')
const revision = ref(''),
  report = ref<unknown>(),
  trial = ref<Trial>()
const preview = ref<ReleasePreview>(),
  approved = ref(false),
  history = ref<{ target: string; previous: string }[]>([])
const trialId = ref('')
const parameters = ref('{}'),
  prompt = ref('')
let alive = true
const outdated = computed(() =>
  Boolean(draft.value && editor.value !== JSON.stringify(draft.value.content, null, 2)),
)
async function request<T>(path: string, method = 'GET', body?: unknown, release = false) {
  return authorRequest<T>(path, key.value, method, body, release ? releaseKey.value : undefined)
}
async function act(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (e) {
    if (alive) error.value = e instanceof Error ? e.message : '操作失败'
  } finally {
    busy.value = false
    releaseKey.value = ''
  }
}
async function refresh() {
  const [catalog, list, releases] = await Promise.all([
    request<{ items: SkillEntry[]; bundle_sha256: string }>('/catalog'),
    request<{ items: typeof drafts.value }>('/drafts'),
    request<{ active: string; history: typeof history.value }>('/releases'),
  ])
  if (!alive) return
  skills.value = catalog.items
  bundle.value = catalog.bundle_sha256
  drafts.value = list.items
  history.value = releases.history
  const linked = route.query.draft
  if (!draft.value && typeof linked === 'string' && /^[a-f0-9]{32}$/.test(linked)) {
    await readDraft(linked)
    if (typeof route.query.trial === 'string') trialId.value = route.query.trial
  }
}
function clearEditor() {
  draft.value = undefined
  editor.value = '{"changes": []}'
  trial.value = undefined
  report.value = undefined
  invalidate()
}
function invalidate() {
  preview.value = undefined
  approved.value = false
}
function showDraft(value: Draft) {
  draft.value = value
  editor.value = JSON.stringify(value.content, null, 2)
  trial.value = undefined
  report.value = undefined
  invalidate()
}
async function readDraft(id: string, rev?: number) {
  showDraft(
    await request<Draft>('/drafts/' + encodeURIComponent(id) + (rev ? '?revision=' + rev : '')),
  )
}
function editSkill(skill: SkillEntry) {
  selected.value = skill
  draft.value = undefined
  trial.value = undefined
  report.value = undefined
  invalidate()
  editor.value = JSON.stringify({ changes: [skill] }, null, 2)
  parameters.value = JSON.stringify(
    Object.fromEntries(skill.contract.inputs.map((x) => [x, ''])),
    null,
    2,
  )
}
async function save() {
  invalidate()
  const content = JSON.parse(editor.value)
  if (editor.value.length > 524288) throw Error('草稿过大')
  const value = await request<Draft>(
    '/drafts' + (draft.value ? '/' + draft.value.draft : ''),
    draft.value ? 'PUT' : 'POST',
    {
      content,
      ...(draft.value
        ? { expected_sha256: draft.value.sha, base: draft.value.base }
        : { base: bundle.value }),
    },
  )
  showDraft(value)
  await refresh()
}
async function inspect(kind: 'diff' | 'history' | 'validate') {
  if (!draft.value) return
  report.value = await request(
    '/drafts/' + draft.value.draft + '/' + kind,
    kind === 'validate' ? 'POST' : 'GET',
  )
}
async function startTrial() {
  if (!draft.value || outdated.value) throw Error('先保存当前草稿')
  invalidate()
  trial.value = await request<Trial>('/drafts/' + draft.value.draft + '/trials', 'POST')
  report.value = trial.value
}
async function readTrial() {
  if (!trial.value && !/^[a-f0-9]{32}$/.test(trialId.value)) throw Error('请输入有效试跑编号')
  trial.value = await request<Trial>('/trials/' + (trial.value?.id ?? trialId.value))
  report.value = trial.value
}
async function prepare(rollback?: string) {
  invalidate()
  if (
    !rollback &&
    (!draft.value ||
      outdated.value ||
      trial.value?.state !== 'passed' ||
      trial.value.revision !== draft.value.revision)
  )
    throw Error('须先保存并完成当前修订试跑')
  preview.value = await request<ReleasePreview>(
    '/releases/preview',
    'POST',
    rollback ? { rollback } : { draft: draft.value!.draft, trial: trial.value!.id },
  )
}
async function confirm() {
  if (
    !preview.value ||
    !approved.value ||
    !releaseKey.value ||
    Date.now() / 1000 >= preview.value.expires
  )
    throw Error('请核对预览、有效期并提供独立发布凭据')
  const p = preview.value
  // Clear before dispatch: ambiguous response requires a new preview, never an automatic retry.
  invalidate()
  report.value = await request(
    '/releases/confirm',
    'POST',
    { preview: p.id, sha256: p.sha256, confirmed: true },
    true,
  )
  await refresh()
}
function apply() {
  if (!selected.value) return
  const values = JSON.parse(parameters.value)
  if (
    !values ||
    typeof values !== 'object' ||
    Array.isArray(values) ||
    JSON.stringify(values).length > 16000
  )
    throw Error('参数须为JSON对象，最多16000字符')
  const allowed = selected.value.contract.inputs
  if (Object.keys(values).some((k) => !allowed.includes(k)))
    throw Error('参数不在当前技能输入契约内')
  if (allowed.some((k) => values[k] === undefined || values[k] === ''))
    throw Error('请补全输入参数')
  prompt.value = `请使用 ${selected.value.name}（版本 ${selected.value.version}，技能包 ${bundle.value}），参数：${JSON.stringify(values)}。核对依赖与参数后先展示方案，等待确认。`
}
function handoff() {
  skillHandoff.value = prompt.value
  void router.push('/assistant')
}
onUnmounted(() => {
  alive = false
  key.value = ''
  releaseKey.value = ''
})
</script>
<template>
  <section class="skills-page" aria-label="技能管理" :aria-busy="busy">
    <h1>技能管理</h1>
    <form class="credentials" @submit.prevent="act(refresh)">
      <input
        v-model="key"
        type="password"
        aria-label="作者凭据"
        placeholder="作者凭据"
        autocomplete="off"
        :disabled="busy"
      />
      <button :disabled="busy">连接 / 刷新</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <div class="skills-grid">
      <aside>
        <h2>技能版本</h2>
        <p class="digest">{{ bundle }}</p>
        <button
          v-for="skill in skills"
          :key="skill.name"
          :disabled="busy || outdated"
          @click="editSkill(skill)"
        >
          {{ skill.name }} · {{ skill.version }}
        </button>
        <h2>草稿</h2>
        <button
          v-for="item in drafts"
          :key="item.id"
          :disabled="busy || outdated"
          @click="act(() => readDraft(item.id))"
        >
          {{ item.id.slice(0, 12) }} · {{ item.head }}
        </button>
      </aside>
      <main>
        <section v-if="selected" class="panel">
          <h2>{{ selected.name }} · {{ selected.version }}</h2>
          <h3>依赖与输入输出</h3>
          <pre>{{
            JSON.stringify(
              {
                requires_skills: selected.requires_skills,
                contract: selected.contract,
                allowed_operations: selected.allowed_operations,
              },
              null,
              2,
            )
          }}</pre>
          <details v-for="(body, name) in selected.files" :key="name">
            <summary>{{ name }}</summary>
            <pre>{{ body }}</pre>
          </details>
          <label
            >组合参数<textarea v-model="parameters" aria-label="组合参数" :disabled="busy" />
          </label>
          <button :disabled="busy" @click="act(async () => apply())">套用参数</button>
          <pre v-if="prompt">{{ prompt }}</pre>
          <button v-if="prompt" :disabled="busy" @click="handoff">带入对话</button>
        </section>
        <section class="panel">
          <h2>
            草稿编辑 <small v-if="draft">修订 {{ draft.revision }}</small>
          </h2>
          <p>已有技能更新须升版。试跑会消耗模型额度；发布须单独确认。</p>
          <textarea
            v-model="editor"
            aria-label="草稿JSON"
            spellcheck="false"
            :disabled="busy || readonly"
            @input="invalidate"
          />
          <div class="actions">
            <button :disabled="busy || readonly" @click="act(save)">
              {{ draft ? '保存修订' : '创建草稿' }}
            </button>
            <button :disabled="busy" @click="clearEditor">清空编辑</button>
            <button :disabled="busy || !draft" @click="act(() => inspect('history'))">
              修订历史
            </button>
            <button :disabled="busy || !draft || outdated" @click="act(() => inspect('diff'))">
              差异比较
            </button>
            <button
              :disabled="busy || !draft || outdated || readonly"
              @click="act(() => inspect('validate'))"
            >
              静态校验
            </button>
          </div>
          <form v-if="draft" @submit.prevent="act(() => readDraft(draft!.draft, Number(revision)))">
            <input
              v-model="revision"
              aria-label="历史修订号"
              type="number"
              min="1"
              :max="draft.revision"
              :disabled="busy"
              required
            /><button :disabled="busy || outdated">读取修订</button>
          </form>
          <label v-if="draft"
            ><input
              v-model="approved"
              type="checkbox"
              :disabled="busy"
            />确认消耗额度执行当前修订试跑</label
          >
          <button
            :disabled="busy || readonly || !draft || outdated || !approved"
            @click="act(startTrial)"
          >
            开始试跑
          </button>
          <input
            v-model="trialId"
            aria-label="试跑编号"
            placeholder="试跑编号（恢复报告）"
            :disabled="busy"
          />
          <button :disabled="busy || (!trial && !trialId)" @click="act(readTrial)">
            刷新试跑报告
          </button>
          <button
            :disabled="busy || readonly || outdated || trial?.state !== 'passed'"
            @click="act(() => prepare())"
          >
            发布预览
          </button>
        </section>
        <details v-if="report" open>
          <summary>结果 / 差异 / 试跑报告</summary>
          <pre>{{ JSON.stringify(report, null, 2) }}</pre>
        </details>
        <section class="panel">
          <h2>发布历史</h2>
          <div v-for="(event, i) in history" :key="i">
            <code>{{ event.target }}</code
            ><button :disabled="busy || readonly" @click="act(() => prepare(event.previous))">
              回滚预览
            </button>
          </div>
        </section>
        <section v-if="preview" class="panel">
          <h2>切换确认</h2>
          <pre>{{ JSON.stringify(preview, null, 2) }}</pre>
          <label
            ><input
              v-model="approved"
              type="checkbox"
              :disabled="busy"
            />已核对目标版本和完整差异</label
          >
          <input
            v-model="releaseKey"
            type="password"
            aria-label="独立发布凭据"
            placeholder="独立发布凭据"
            autocomplete="off"
            :disabled="busy"
          />
          <button :disabled="busy || readonly || !approved || !releaseKey" @click="act(confirm)">
            确认切换
          </button>
        </section>
      </main>
    </div>
  </section>
</template>
<style scoped>
.skills-page {
  padding: 28px;
  max-width: 1500px;
  margin: auto;
}
.skills-grid {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 24px;
}
.skills-grid aside button {
  display: block;
  width: 100%;
  text-align: left;
  margin: 8px 0;
  overflow-wrap: anywhere;
}
.credentials,
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
textarea {
  width: 100%;
  min-height: 160px;
  box-sizing: border-box;
  font-family: monospace;
}
input,
textarea {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  padding: 10px;
}
pre,
code,
.digest {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
main {
  min-width: 0;
}
.panel {
  padding: 18px;
  border: 1px solid var(--line);
  margin-bottom: 18px;
}
button {
  margin: 4px;
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface-raised);
  color: var(--text);
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
label {
  display: block;
  margin: 12px 0;
}
h2 {
  font-size: 18px;
}
@media (max-width: 800px) {
  .skills-grid {
    grid-template-columns: 1fr;
  }
  .skills-page {
    padding: 16px;
  }
}
</style>
