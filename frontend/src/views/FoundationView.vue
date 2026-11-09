<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import { listBatches, type BatchPage } from '@/api/client'
import AsyncStatePanel from '@/components/AsyncStatePanel.vue'
import { useFilterStore } from '@/stores/filters'

type CheckState = 'idle' | 'loading' | 'empty' | 'error' | 'success'

const filters = useFilterStore()
const checkState = ref<CheckState>('idle')
const checkError = ref<unknown>()
const batchResult = ref<BatchPage>()
let activeController: AbortController | undefined

onBeforeUnmount(() => activeController?.abort())

const stateCards = [
  {
    index: '01',
    title: '接口契约',
    value: 'OpenAPI 3.1',
    note: '由后端应用实际生成，前端类型不靠手写猜测。',
  },
  {
    index: '02',
    title: '请求治理',
    value: '10s Timeout',
    note: '支持取消、网络异常、HTTP 错误和 Request ID 追踪。',
  },
  {
    index: '03',
    title: '状态管理',
    value: 'Pinia + URL',
    note: '筛选条件写入地址栏，可刷新、收藏和分享。',
  },
  {
    index: '04',
    title: '质量基线',
    value: 'Unit + E2E',
    note: 'Vitest、Vue Test Utils 与 Playwright 已接入。',
  },
]

const stateForPanel = computed(() => (checkState.value === 'success' ? 'idle' : checkState.value))

async function checkApi() {
  activeController?.abort()
  activeController = new AbortController()
  checkState.value = 'loading'
  checkError.value = undefined

  try {
    const result = await listBatches({ limit: 1, offset: 0 }, { signal: activeController.signal })
    batchResult.value = result
    checkState.value = result.items.length ? 'success' : 'empty'
  } catch (error) {
    checkError.value = error
    checkState.value = 'error'
  }
}
</script>

<template>
  <section class="page foundation-page">
    <div class="page-intro">
      <div>
        <span class="eyebrow">M3-V0 · Frontend foundation</span>
        <h1>工程基础已经接通</h1>
        <p>
          当前版本先建立前端运行、接口访问与质量保障的共同底座。总览、航班检索和预测分析将在后续里程碑逐步接入。
        </p>
      </div>
      <div class="release-stamp">
        <span>BASELINE</span>
        <strong>V0</strong>
        <small>2026 · M3</small>
      </div>
    </div>

    <div class="metric-grid" aria-label="工程能力概览">
      <article v-for="card in stateCards" :key="card.index" class="metric-card">
        <span class="metric-index">{{ card.index }}</span>
        <div>
          <small>{{ card.title }}</small>
          <strong>{{ card.value }}</strong>
          <p>{{ card.note }}</p>
        </div>
      </article>
    </div>

    <div class="workspace-grid">
      <article class="panel filter-panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">GLOBAL CONTEXT</span>
            <h2>全局筛选上下文</h2>
          </div>
          <button v-if="filters.activeCount" class="text-button" @click="filters.reset()">
            清空 {{ filters.activeCount }} 项
          </button>
        </header>

        <div class="filter-grid">
          <label>
            <span>数据批次</span>
            <el-input v-model="filters.batch" clearable placeholder="scat20161015_20161021" />
          </label>
          <label>
            <span>航班号</span>
            <el-input v-model="filters.callsign" clearable placeholder="例如 CCA1234" />
          </label>
          <label>
            <span>起飞机场</span>
            <el-input v-model="filters.adep" clearable placeholder="ICAO 四字码" maxlength="4" />
          </label>
          <label>
            <span>降落机场</span>
            <el-input v-model="filters.ades" clearable placeholder="ICAO 四字码" maxlength="4" />
          </label>
        </div>
        <p class="helper-text">
          修改后会自动同步到 URL 查询参数；页面刷新或分享链接时，筛选上下文仍可恢复。
        </p>
      </article>

      <article class="panel api-panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">CONTRACT CHECK</span>
            <h2>后端接口检查</h2>
          </div>
          <el-button type="primary" :loading="checkState === 'loading'" @click="checkApi">
            {{ checkState === 'success' ? '再次检查' : '检查连接' }}
          </el-button>
        </header>

        <div v-if="checkState === 'success'" class="api-success">
          <div class="success-pulse"><i></i></div>
          <div>
            <strong>SCAT 接口响应正常</strong>
            <p>
              已读取 {{ batchResult?.pagination.total ?? 0 }} 个批次；首个批次为
              <code>{{ batchResult?.items[0]?.batch ?? '—' }}</code
              >。
            </p>
          </div>
        </div>
        <AsyncStatePanel
          v-else
          :state="stateForPanel"
          :error="checkError"
          empty-description="接口可访问，但当前没有可用批次。"
          @retry="checkApi"
        />
      </article>
    </div>

    <article class="panel delivery-panel">
      <header class="panel-header">
        <div>
          <span class="panel-kicker">DELIVERY MAP</span>
          <h2>M3 视觉化交付路径</h2>
        </div>
        <span class="live-label"><i></i> 当前 M3-V0</span>
      </header>
      <div class="delivery-track">
        <div class="delivery-step active"><b>V0</b><span>工程基础</span></div>
        <div class="delivery-step"><b>V1</b><span>总览工作台</span></div>
        <div class="delivery-step"><b>V2–V4</b><span>航迹与计划</span></div>
        <div class="delivery-step"><b>V5–V6</b><span>评估与助手</span></div>
        <div class="delivery-step"><b>V7–V10</b><span>优化与验收</span></div>
      </div>
    </article>
  </section>
</template>
