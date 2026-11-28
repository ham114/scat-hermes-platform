<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
import { memoryRequest, type ChartPreferences, type ChartPreference } from '@/api/memory'
import { newRequestId } from '@/api/hermes'
const props = defineProps<{
  epoch: number
  enabled: boolean
  revision?: number
  readOnly?: boolean
}>()
const emit = defineEmits<{ changed: [] }>()
const state = ref<ChartPreferences>()
const key = ref('palette'),
  value = ref(''),
  taskType = ref('all'),
  error = ref(''),
  notice = ref(''),
  busy = ref(false)
const labels: Record<string, string> = {
  palette: '配色',
  font_family: '字体',
  font_size: '字号',
  density: '布局密度',
  legend_position: '图例位置',
  units: '单位习惯',
  altitude_unit: '高度单位',
  chart_style: '图形习惯',
  special_needs: '特殊需求',
}
const scopes: Record<string, string> = {
  all: '所有图表',
  general: '一般分析',
  trajectory: '轨迹图',
  prediction: '预测图',
  classification: '分类图',
}
const editableLabels = computed(() => ({
  ...labels,
  ...Object.fromEntries(
    (state.value?.items ?? [])
      .filter((item) => item.key.startsWith('rule_'))
      .map((item) => [item.key, `其他规则：${item.value}`]),
  ),
}))
const preferenceLabel = (key: string) =>
  labels[key] ?? (key.startsWith('rule_') ? '其他绘图规则' : key)
const values: Record<string, string> = {
  blue: '蓝色系',
  red: '红色系',
  green: '绿色系',
  avoid_red: '避开红色',
  avoid_blue: '避开蓝色',
  avoid_green: '避开绿色',
  warm: '暖色系',
  cool: '冷色系',
  colorblind_safe: '色盲友好',
  top: '顶部',
  bottom: '底部',
  left: '左侧',
  right: '右侧',
  compact: '紧凑',
  spacious: '宽松',
  normal: '普通',
  line: '折线图',
  bar: '柱状图',
  scatter: '散点图',
  heatmap: '热力图',
  m: '米',
  ft: '英尺',
  km: '公里',
}
const pending = ref<{
  request_id: string
  epoch: number
  revision: number
  key: string
  value: string | number | null
  task_type: string
}>()
let generation = 0
let alive = true
async function load() {
  const token = ++generation
  try {
    const result = await memoryRequest<ChartPreferences>('/chart-preferences')
    if (!alive || token !== generation) return
    if (!Array.isArray(result.items)) throw Error('invalid_preferences')
    if (
      pending.value &&
      (pending.value.epoch !== result.epoch || pending.value.revision !== result.revision)
    ) {
      pending.value = undefined
      notice.value = '记忆版本已变化，请核对当前值后再修改。'
    }
    state.value = result
    error.value = ''
  } catch {
    if (alive && token === generation) error.value = '图表偏好暂不可用，请刷新读取。'
  }
}
watch(
  () => [props.epoch, props.enabled, props.revision],
  (current, previous) => {
    if (previous && current[0] !== previous[0]) {
      notice.value = value.value || pending.value ? '旧偏好输入已清除。' : ''
      value.value = ''
      pending.value = undefined
    }
    state.value = undefined
    void load()
  },
  { immediate: true },
)
onUnmounted(() => {
  alive = false
  generation++
  pending.value = undefined
})
async function submit() {
  if (!pending.value || busy.value || props.readOnly) return
  const submitted = pending.value
  const submittedGeneration = ++generation
  busy.value = true
  error.value = ''
  try {
    const result = await memoryRequest<ChartPreferences>('/chart-preferences', 'POST', submitted)
    if (!alive || submittedGeneration !== generation || props.epoch !== submitted.epoch) return
    if (!Array.isArray(result.items)) throw Error('invalid_preferences')
    if (result.epoch !== submitted.epoch) {
      await load()
      emit('changed')
      return
    }
    state.value = result
    pending.value = undefined
    value.value = ''
    notice.value =
      submitted.value === null
        ? '已删除这项本地图表偏好。历史图表保持可读。'
        : '图表偏好已在本地保存，后续生成立即使用；远端摘要可能稍后更新。'
    emit('changed')
  } catch {
    if (alive && submittedGeneration === generation)
      error.value = '更正结果待核实，请先刷新当前值；恢复操作会保留原请求号。'
  } finally {
    busy.value = false
  }
}
function save() {
  if (!state.value || pending.value || !state.value.enabled) return
  let parsed = key.value === 'font_size' ? Number(value.value) : value.value.trim()
  if (key.value === 'altitude_unit') {
    const aliases: Record<string, string> = { 米: 'm', 英尺: 'ft', 公里: 'km', 千米: 'km' }
    const unit = String(parsed).toLowerCase()
    parsed = aliases[String(parsed)] ?? (['m', 'ft', 'km'].includes(unit) ? unit : parsed)
  }
  if (
    key.value === 'font_size' &&
    (!Number.isInteger(parsed) || Number(parsed) < 8 || Number(parsed) > 40)
  ) {
    error.value = '字号请输入 8 至 40 的整数。'
    return
  }
  if (typeof parsed === 'string' && !parsed) return
  pending.value = {
    request_id: newRequestId(),
    epoch: state.value.epoch,
    revision: state.value.revision,
    key: key.value,
    value: parsed,
    task_type: taskType.value,
  }
  void submit()
}
function remove(item: ChartPreference) {
  if (!state.value || pending.value || busy.value) return
  pending.value = {
    request_id: newRequestId(),
    epoch: state.value.epoch,
    revision: state.value.revision,
    key: item.key,
    value: null,
    task_type: item.task_type,
  }
  void submit()
}
function edit(item: ChartPreference) {
  key.value = item.key
  value.value = String(item.value)
  taskType.value = item.task_type
}
</script>

<template>
  <section class="chart-preferences" aria-label="图表长期偏好">
    <h2>图表长期偏好</h2>
    <p>
      模型根据原话区分当前图表要求和长期默认偏好；只有明确要求长期使用的偏好才保存。请在这里核对保存结果和依据。
    </p>
    <p v-if="state && !state.enabled">记忆已暂停：保存的偏好不会用于新图，也不会记录新偏好。</p>
    <button :disabled="busy" @click="load">刷新图表偏好</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="state && !state.items.length">尚无已保存的图表偏好。</p>
    <ul v-if="state?.items.length">
      <li v-for="item in state.items" :key="item.id">
        <strong
          >{{ scopes[item.task_type] ?? item.task_type }} · {{ preferenceLabel(item.key) }}：{{
            values[String(item.value)] ?? item.value
          }}</strong
        >
        <details v-if="item.evidence || item.source_artifact">
          <summary>查看依据</summary>
          <p>{{ item.evidence }}</p>
          <p v-if="item.source_artifact">来源图表：{{ item.source_artifact }}</p>
        </details>
        <button :disabled="busy || readOnly || !!pending || !state.enabled" @click="edit(item)">
          更正{{ preferenceLabel(item.key) }}
        </button>
        <button :disabled="busy || readOnly || !!pending" @click="remove(item)">
          删除{{ preferenceLabel(item.key) }}偏好
        </button>
      </li>
    </ul>
    <form @submit.prevent="save">
      <p v-if="taskType !== 'all'">正在更正已有的{{ scopes[taskType] ?? taskType }}记录。</p>
      <label
        >偏好项目<select
          v-model="key"
          :disabled="busy || readOnly || !!pending"
          aria-label="图表偏好项目"
        >
          <option v-for="(label, id) in editableLabels" :key="id" :value="id">{{ label }}</option>
        </select></label
      >
      <label
        >偏好值<input
          v-model="value"
          aria-label="图表偏好值"
          :maxlength="key === 'special_needs' ? 300 : 80"
          :disabled="busy || readOnly || !!pending"
          placeholder="例如：米、未说明单位时默认用米、蓝色系，或字号 18"
      /></label>
      <button
        type="submit"
        :disabled="busy || readOnly || !!pending || !state?.enabled || !value.trim()"
      >
        保存图表偏好
      </button>
    </form>
    <div v-if="pending">
      <button :disabled="busy || readOnly" @click="submit">恢复原偏好请求</button
      ><button :disabled="busy" @click="pending = undefined">结束恢复并核对当前值</button>
    </div>
    <p>
      本次要求优先于当前图表、特定任务和全局偏好。偏好不能覆盖数据真实性和审核要求；清空长期记忆也会清除本地偏好与依据原话。
    </p>
  </section>
</template>

<style scoped>
.chart-preferences {
  border: 1px solid var(--line);
  padding: 20px;
  border-radius: 12px;
  margin: 20px 0;
  min-width: 0;
}
label {
  display: block;
  margin: 10px 0;
}
input,
select,
button {
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
  max-width: 100%;
  box-sizing: border-box;
}
input {
  width: 100%;
  display: block;
  margin-top: 6px;
}
button {
  margin: 4px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
li {
  border-top: 1px solid var(--line);
  padding: 12px 0;
}
ul {
  list-style: none;
  padding: 0;
}
p,
strong {
  overflow-wrap: anywhere;
}
h2 {
  font-size: 18px;
}
</style>
