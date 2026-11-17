<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  chartAxes,
  chartColumns,
  chartGroups,
  chartMetrics,
  chartRows,
  defaultChart,
  resultChart,
  resultCsv,
  validChart,
  type ChartInput,
  type ChartKind,
  type ChartView,
} from '@/lib/analysisChart'
import { withUnit } from '@/lib/analysisDisplay'
import { downloadCsv } from '@/utils/evaluationExport'
import BaseChart from './BaseChart.vue'

const props = defineProps<{ input: ChartInput; view?: ChartView; title: string; ordinal: number }>()
const emit = defineEmits<{ change: [value: ChartView | undefined] }>()
const choices: { kind: ChartKind; label: string }[] = [
  { kind: 'line', label: '折线图' },
  { kind: 'bar', label: '柱状图' },
  { kind: 'scatter', label: '散点图' },
  { kind: 'heatmap', label: '热力图' },
  { kind: 'boxplot', label: '箱线图' },
]
const options = computed(() => choices.filter((choice) => defaultChart(props.input, choice.kind)))
const active = computed(() => (validChart(props.input, props.view) ? props.view : undefined))
const axes = computed(() => (active.value ? chartAxes(props.input, active.value.kind) : []))
const metrics = computed(() =>
  active.value ? chartMetrics(props.input, active.value.kind, active.value.x) : [],
)
const groups = computed(() => (active.value ? chartGroups(props.input, active.value) : []))
const option = computed(() => (active.value ? resultChart(props.input, active.value) : undefined))
const rows = computed(() => (active.value ? chartRows(props.input, active.value) : []))
const drawn = computed(() =>
  active.value ? rows.value.filter((row) => row[active.value!.y] != null).length : 0,
)
const chart = ref<InstanceType<typeof BaseChart>>()
const container = ref<HTMLElement>()
const error = ref('')
const selected = (event: Event) => (event.target as HTMLSelectElement).value

function choose(event: Event) {
  const kind = options.value.find((choice) => choice.kind === selected(event))?.kind
  emit('change', kind ? defaultChart(props.input, kind) : undefined)
}

function edit(field: 'x' | 'y' | 'group', event: Event) {
  const view = active.value
  if (!view) return
  const value = selected(event)
  const next = { ...view, [field]: value || undefined }
  if (validChart(props.input, next)) emit('change', next)
  else if (field === 'x') emit('change', defaultChart(props.input, view.kind, value))
  else if (field === 'y') {
    const changed = { ...view, y: value }
    const compatible = chartGroups(props.input, changed)
    emit('change', { ...changed, group: view.kind === 'heatmap' ? compatible[0]?.key : undefined })
  }
}

function exportData() {
  const view = active.value
  if (!view) return
  downloadCsv(
    resultCsv(chartColumns(props.input, view), rows.value),
    `analysis-chart-${props.ordinal}.csv`,
  )
}

function exportImage() {
  const url = chart.value?.imageUrl()
  if (!url) return
  const link = document.createElement('a')
  link.href = url
  link.download = `analysis-chart-${props.ordinal}.png`
  link.click()
}

async function fullscreen() {
  error.value = ''
  try {
    if (document.fullscreenElement === container.value) await document.exitFullscreen()
    else await container.value?.requestFullscreen()
  } catch {
    error.value = '当前浏览器无法进入图表全屏。'
  }
}
</script>

<template>
  <div v-if="options.length" ref="container" class="chart-view">
    <div class="selectors">
      <label
        >图表类型
        <select :value="active?.kind ?? ''" @change="choose">
          <option value="">原始展示</option>
          <option v-for="choice in options" :key="choice.kind" :value="choice.kind">
            {{ choice.label }}
          </option>
        </select>
      </label>
      <template v-if="active">
        <label
          >横轴
          <select :value="active.x" @change="edit('x', $event)">
            <option v-for="field in axes" :key="field.key" :value="field.key">
              {{ withUnit(field.label, field.unit) }}
            </option>
          </select>
        </label>
        <label
          >{{ active.kind === 'heatmap' ? '颜色指标' : '纵轴' }}
          <select :value="active.y" @change="edit('y', $event)">
            <option v-for="field in metrics" :key="field.key" :value="field.key">
              {{ withUnit(field.label, field.unit) }}
            </option>
          </select>
        </label>
        <label v-if="groups.length"
          >{{ active.kind === 'heatmap' ? '纵轴分组' : '分组' }}
          <select :value="active.group ?? ''" @change="edit('group', $event)">
            <option v-if="active.kind !== 'heatmap'" value="">不分组</option>
            <option v-for="field in groups" :key="field.key" :value="field.key">
              {{ field.label }}
            </option>
          </select>
        </label>
      </template>
    </div>
    <template v-if="option">
      <p class="coverage">
        当前结果 {{ input.rows.length }} 行 · 有效绘制 {{ drawn }} 行 · CSV 包含当前所选字段
      </p>
      <BaseChart
        ref="chart"
        :option="option"
        height="var(--analysis-chart-height, 300px)"
        :label="`${title} · ${choices.find((choice) => choice.kind === active?.kind)?.label}`"
      />
      <div class="selectors">
        <button type="button" @click="exportData">下载图表数据 CSV</button>
        <button type="button" @click="exportImage">下载图表 PNG</button>
        <button type="button" @click="fullscreen">切换图表全屏</button>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
    </template>
  </div>
</template>

<style scoped>
.chart-view {
  margin-block: 8px;
  min-width: 0;
}
.chart-view:fullscreen {
  --analysis-chart-height: max(300px, calc(100dvh - 160px));
  overflow: auto;
  background: var(--panel, white);
  padding: 16px;
}
.selectors {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}
.selectors label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  max-width: 100%;
}
.selectors select {
  max-width: min(45vw, 210px);
}
.coverage {
  color: var(--muted);
  font-size: 0.85rem;
}
</style>
