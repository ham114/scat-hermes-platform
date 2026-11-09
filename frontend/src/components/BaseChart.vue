<script setup lang="ts">
import { BarChart, LineChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

use([
  BarChart,
  LineChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption
    height?: string
    label: string
  }>(),
  { height: '300px' },
)

const root = ref<HTMLElement>()
let chart: ECharts | undefined
let observer: ResizeObserver | undefined

function updateChart() {
  chart?.setOption(props.option, { notMerge: true })
}

onMounted(() => {
  if (!root.value) return
  chart = init(root.value)
  updateChart()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(root.value)
})

watch(() => props.option, updateChart, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div ref="root" class="base-chart" :style="{ height }" role="img" :aria-label="label"></div>
</template>
