<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import BaseChart from './BaseChart.vue'
import { scalarLabel, trajectoryChart, withUnit, type TrajectoryBlock } from '@/lib/analysisDisplay'

const props = defineProps<{ block: TrajectoryBlock }>()
const expanded = ref(false)
const Workspace = defineAsyncComponent(() => import('./AnalysisTrajectoryWorkspace.vue'))
const StreetMap = defineAsyncComponent(() => import('./AnalysisTrajectoryMap.vue'))
const geographic = computed(
  () =>
    ['degree', 'degrees', 'deg', '°'].includes(props.block.x_unit ?? '') &&
    ['degree', 'degrees', 'deg', '°'].includes(props.block.y_unit ?? ''),
)
const snapshot = ref('')
const options = computed(() =>
  props.block.snapshot_key
    ? [...new Set(props.block.points.map((point) => scalarLabel(point[props.block.snapshot_key!])))]
    : [],
)
watch(
  () => props.block,
  () => {
    snapshot.value = options.value[0] ?? ''
  },
  { immediate: true },
)
const option = computed(() => trajectoryChart(props.block, snapshot.value))
const points = computed(() =>
  props.block.points.filter(
    (point) =>
      !props.block.snapshot_key || scalarLabel(point[props.block.snapshot_key]) === snapshot.value,
  ),
)
const columns = computed(() => [
  { key: props.block.x_key, label: withUnit(props.block.x_label, props.block.x_unit) },
  { key: props.block.y_key, label: withUnit(props.block.y_label, props.block.y_unit) },
  ...(props.block.time_key ? [{ key: props.block.time_key, label: '时间' }] : []),
  ...(props.block.path_key ? [{ key: props.block.path_key, label: props.block.path_label! }] : []),
  ...(props.block.segment_key
    ? [{ key: props.block.segment_key, label: props.block.segment_label! }]
    : []),
  ...props.block.series.map((item) => ({ key: item.key, label: withUnit(item.label, item.unit) })),
])
</script>

<template>
  <label v-if="options.length" class="snapshot-control">
    {{ block.snapshot_label }}
    <select v-model="snapshot" :aria-label="block.snapshot_label ?? '轨迹快照'">
      <option v-for="item in options" :key="item" :value="item">{{ item }}</option>
    </select>
  </label>
  <StreetMap v-if="geographic && block.points.length && !expanded" :block="{ ...block, points }" />
  <BaseChart v-else-if="!geographic && block.points.length" :option="option" :label="block.title" />
  <p v-else-if="!block.points.length">当前查询没有可展示的轨迹点。</p>
  <button v-if="geographic" @click="expanded = !expanded">
    {{ expanded ? '收起轨迹交互' : '打开轨迹交互' }}
  </button>
  <Workspace v-if="expanded" :block="block" />
  <p class="trajectory-note">按原始点序列连接，路径与分段分别展示；无效坐标保留为空缺。</p>
  <details v-if="points.length">
    <summary>轨迹数值明细 · {{ points.length }} 点（表格最多展示前200点）</summary>
    <div class="trajectory-values">
      <table>
        <thead>
          <tr>
            <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(point, index) in points.slice(0, 200)" :key="index">
            <td v-for="column in columns" :key="column.key">
              {{ scalarLabel(point[column.key]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>
</template>

<style scoped>
.snapshot-control {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
select {
  color: var(--text);
  background: var(--surface-raised);
  border: 1px solid var(--line);
  padding: 6px;
}
.trajectory-note {
  color: var(--text-muted);
  font-size: 12px;
}
.trajectory-values {
  overflow: auto;
  max-width: 100%;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  text-align: left;
}
th,
td {
  padding: 8px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
</style>
