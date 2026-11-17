<script setup lang="ts">
import { BarChart, BoxplotChart, HeatmapChart, LineChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { init, use, type ECharts, type EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

use([
  BarChart,
  BoxplotChart,
  LineChart,
  ScatterChart,
  HeatmapChart,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  DataZoomComponent,
  VisualMapComponent,
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

function imageUrl() {
  return chart?.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#fff' })
}

defineExpose({ imageUrl })

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
