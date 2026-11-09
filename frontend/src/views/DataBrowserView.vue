<script setup lang="ts">
import { computed, ref } from 'vue'
import WorkspaceIcon from '@/components/WorkspaceIcon.vue'
import { parseArtifactReference } from '@/utils/crossAppNavigation'
const input = ref('')
const reference = computed(() => parseArtifactReference(input.value))
const entries = [
  { to: '/overview', icon: 'data', title: '航班总览', subtitle: '批次与数据质量' },
  { to: '/flights', icon: 'flow', title: '航班检索', subtitle: '航班与轨迹' },
  { to: '/evaluation', icon: 'file', title: '预测评估', subtitle: '指标与分析结果' },
]
</script>
<template>
  <section class="workspace-page data-browser">
    <header class="workspace-page-heading">
      <span class="workspace-kicker">浏览</span>
      <h1>数据与结果</h1>
    </header>
    <div class="data-browser-grid">
      <router-link
        v-for="entry in entries"
        :key="entry.to"
        :to="entry.to"
        class="data-browser-card"
      >
        <WorkspaceIcon :name="entry.icon" :size="25" />
        <h2>{{ entry.title }}</h2>
        <p>{{ entry.subtitle }}</p>
        <WorkspaceIcon name="chevron" :size="18" />
      </router-link>
    </div>
    <section class="result-entry">
      <h2>打开已有结果</h2>
      <label class="sr-only" for="artifact-reference">结果编号或链接</label>
      <div class="result-entry-form">
        <input
          id="artifact-reference"
          v-model="input"
          placeholder="粘贴结果编号或链接"
          autocomplete="off"
          spellcheck="false"
        />
        <router-link
          v-if="reference"
          class="primary-link artifact-open-link"
          :to="{
            name: 'visualization-3d',
            params: { artifactId: reference.artifactId },
            query: reference.threadId ? { thread_id: reference.threadId } : undefined,
          }"
          >打开结果</router-link
        >
      </div>
      <p v-if="input && !reference" role="status">请输入有效的结果编号或链接。</p>
    </section>
  </section>
</template>
