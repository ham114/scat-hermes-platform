<script setup lang="ts">
import { computed, inject, onUnmounted, ref, watch, toRaw } from 'vue'
import { chatDisplayKey } from '@/lib/chatDisplay'
import { defaultDisplay, parseDisplay, displayCommand, displayBlocks } from '@/lib/displayWorkspace'
import BaseChart from './BaseChart.vue'
import AnalysisTrajectoryBlock from './AnalysisTrajectoryBlock.vue'
import {
  blockChart,
  safeAnalysisLink,
  scalarLabel,
  withUnit,
  type AnalysisResult,
} from '@/lib/analysisDisplay'

const props = defineProps<{
  result: AnalysisResult
  artifactId?: string
  payloadSha256?: string
  displayIdentity?: string
}>()
const config = ref(defaultDisplay(props.result))
const command = ref('')
const notice = ref('')
const chatDisplay = inject(chatDisplayKey, undefined)
let displayBinding: ReturnType<NonNullable<typeof chatDisplay>['register']> | undefined
const storageKey = computed(() =>
  props.displayIdentity
    ? 'hermes-display-receipts-v1:' + props.displayIdentity
    : props.artifactId && props.payloadSha256
      ? 'hermes-display-v1:' + props.artifactId + ':' + props.payloadSha256
      : null,
)
watch(
  () => [props.result, storageKey.value],
  () => {
    displayBinding?.dispose()
    config.value = defaultDisplay(props.result)
    notice.value = ''
    try {
      const saved = storageKey.value && localStorage.getItem(storageKey.value)
      if (saved) config.value = parseDisplay(JSON.parse(saved), props.result)
    } catch {
      notice.value = '无法恢复展示配置，已使用默认布局'
    }
    displayBinding = chatDisplay?.register((text) => {
      const next = displayCommand(text, toRaw(config.value), props.result)
      config.value = next
      if (storageKey.value) {
        try {
          localStorage.setItem(storageKey.value, JSON.stringify(next))
          notice.value = '展示已调整并保存到当前浏览器，原始结果未变'
        } catch {
          notice.value = '展示已调整，但浏览器存储不可用，尚未保存'
        }
      } else notice.value = '展示已调整；当前结果缺少固定引用，尚未保存'
      return notice.value
    })
  },
  { immediate: true },
)
onUnmounted(() => displayBinding?.dispose())
const visible = computed(() => displayBlocks(props.result, toRaw(config.value)))
function apply() {
  try {
    config.value = displayCommand(command.value, toRaw(config.value), props.result)
    notice.value = '展示已调整，原始结果未变'
  } catch (e) {
    notice.value = e instanceof Error ? e.message : '调整失败'
  }
}
function persist() {
  if (!storageKey.value) {
    notice.value = '当前结果缺少固定引用，不能保存'
    return
  }
  try {
    localStorage.setItem(storageKey.value, JSON.stringify(config.value))
    notice.value = '已保存到当前浏览器'
  } catch {
    notice.value = '浏览器存储不可用，尚未保存'
  }
}
function chart(block: Parameters<typeof blockChart>[0]) {
  const option = blockChart(block)
  if (option && config.value.chart !== 'default' && Array.isArray(option.series))
    option.series = option.series.map((series) => ({
      ...series,
      type: config.value.chart === 'bar' ? ('bar' as const) : ('line' as const),
    }))
  return option
}
const link = (url: string) => safeAnalysisLink(url, window.location.origin)
</script>

<template>
  <section
    class="analysis-result"
    aria-label="结构化分析结果"
    @pointerdown="displayBinding?.activate()"
    @focusin="displayBinding?.activate()"
  >
    <header>
      <h3>{{ result.title }}</h3>
      <p>{{ result.dataset }} · {{ result.operation }} · 协议 {{ result.schema_version }}</p>
      <p v-if="result.start_time">起始时间（UTC）：{{ result.start_time }}</p>
      <small v-if="artifactId">结果引用：{{ artifactId }}</small>
      <small v-if="payloadSha256">内容摘要：{{ payloadSha256 }}</small>
    </header>
    <form class="display-controls" aria-label="调整展示" @submit.prevent="apply">
      <input
        v-model="command"
        aria-label="展示调整"
        placeholder="双列；置顶 2；降序 2 字段；柱状图"
        maxlength="500"
      />
      <button type="submit">调整展示</button
      ><button type="button" @click="persist">保存展示</button>
      <details>
        <summary>展示指令</summary>
        <p>
          单列 / 双列；置顶 2；隐藏 3 / 显示 3；升序 2 字段；只看 2 字段1 字段2；折线图 / 柱状图 /
          默认图表；恢复默认。编号为原始内容块顺序。排序仅限当前已加载表格页。
        </p>
      </details>
      <p v-if="notice" role="status">{{ notice }}</p>
    </form>
    <div class="display-grid" :class="{ dual: config.columns === 2 }">
      <article
        v-for="{ block, id } in visible"
        :key="id"
        class="result-block"
        :data-block-type="block.type"
      >
        <small>内容 {{ id + 1 }}</small>
        <h4 v-if="'title' in block">{{ block.title }}</h4>
        <dl v-if="block.type === 'metrics'" class="result-metrics">
          <div v-for="item in block.items" :key="item.key">
            <dt>{{ item.label }}</dt>
            <dd>{{ withUnit(scalarLabel(item.value), item.unit) }}</dd>
          </div>
        </dl>
        <template v-else-if="block.type === 'table'">
          <div
            v-if="block.rows.length"
            class="result-scroll"
            tabindex="0"
            :aria-label="block.title"
          >
            <table>
              <thead>
                <tr>
                  <th v-for="column in block.columns" :key="column.key" scope="col">
                    {{ withUnit(column.label, column.unit) }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
                  <td v-for="column in block.columns" :key="column.key">
                    {{ scalarLabel(row[column.key]) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else>当前查询没有可展示的记录。</p>
        </template>
        <AnalysisTrajectoryBlock v-else-if="block.type === 'trajectory_2d'" :block="block" />
        <template
          v-else-if="
            block.type === 'timeseries' ||
            block.type === 'classification' ||
            block.type === 'prediction'
          "
        >
          <BaseChart
            v-if="(block.type === 'classification' ? block.items : block.points).length"
            :option="chart(block)!"
            :label="block.title"
          />
          <p v-else>当前查询没有可展示的数据。</p>
          <p v-if="block.type === 'prediction'">
            预测来源：{{ block.source }}；时间轴为相对预测时距，不代表实际观测。
          </p>
          <div v-if="block.type === 'classification' && block.items.length" class="result-scroll">
            <table>
              <thead>
                <tr>
                  <th>类别</th>
                  <th>数量</th>
                  <th>置信度</th>
                  <th>样本引用</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(item, itemIndex) in block.items" :key="itemIndex">
                  <td>{{ item.label }}</td>
                  <td>{{ item.count ?? 1 }}</td>
                  <td>{{ scalarLabel(item.confidence) }}</td>
                  <td>{{ scalarLabel(item.sample_ref) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <template v-else-if="block.type === 'action_link'">
          <a v-if="link(block.url)" :href="link(block.url)!" rel="noopener noreferrer"
            >{{ block.label }} ↗</a
          >
          <p v-else role="status">{{ block.label }}：链接不符合展示规则，请从平台入口打开。</p>
        </template>
      </article>
    </div>
  </section>
</template>

<style scoped>
.display-controls {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 16px;
}
.display-controls input {
  flex: 1;
  min-width: 180px;
  padding: 10px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
}
.display-controls p {
  width: 100%;
  font-size: 13px;
}
.display-grid {
  display: grid;
  gap: 12px;
  min-width: 0;
}
.display-grid.dual {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
@media (max-width: 800px) {
  .display-grid.dual {
    grid-template-columns: minmax(0, 1fr);
  }
}

.analysis-result {
  min-width: 0;
  border-top: 1px solid var(--line);
  margin-top: 20px;
  padding-top: 16px;
}
header p,
header small {
  color: var(--text-muted);
  overflow-wrap: anywhere;
  display: block;
}
.result-block {
  margin: 18px 0;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 10px;
  min-width: 0;
}
h3,
h4 {
  margin-top: 0;
  overflow-wrap: anywhere;
}
.result-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  margin: 0;
}
dt {
  color: var(--text-muted);
  font-size: 13px;
}
dd {
  margin: 6px 0 0;
  font-size: 22px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.result-scroll {
  max-width: 100%;
  overflow: auto;
}
table {
  width: max-content;
  min-width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
}
th,
td {
  border-bottom: 1px solid var(--line);
  padding: 10px;
  white-space: nowrap;
  overflow-wrap: normal;
}
@media (max-width: 600px) {
  .result-block {
    padding: 10px;
  }
  .result-metrics {
    gap: 12px;
  }
}
</style>
