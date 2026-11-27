<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import {
  readVisualization,
  visualizationFile,
  type VisualizationArtifact,
} from '@/api/visualizations'
const props = defineProps<{ threadId: string; artifactId: string; readOnly?: boolean }>()
const emit = defineEmits<{ revise: [artifactId: string, feedback: string] }>()
const artifact = ref<VisualizationArtifact>()
const feedback = ref('')
const error = ref('')
const loading = ref(false)
let generation = 0
let controller: AbortController | undefined
watch(
  () => [props.threadId, props.artifactId],
  async () => {
    const token = ++generation
    controller?.abort()
    controller = new AbortController()
    artifact.value = undefined
    error.value = ''
    feedback.value = ''
    loading.value = true
    try {
      const result = await readVisualization(props.threadId, props.artifactId, controller.signal)
      if (token === generation) artifact.value = result
    } catch {
      if (token === generation) error.value = '图表暂时不可用或来源已撤权。'
    } finally {
      if (token === generation) loading.value = false
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  generation++
  controller?.abort()
})
</script>

<template>
  <section class="visualization-artifact" aria-label="已审核自主图表">
    <p v-if="loading" role="status">正在读取图表…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="artifact && !error">
      <h3>自主图表 · 版本 {{ artifact.version }}</h3>
      <p class="intent">{{ artifact.intent }}</p>
      <p>已通过独立模型视觉审核 · 数据范围见来源回执</p>
      <picture>
        <source
          v-if="artifact.viewports.includes('mobile')"
          media="(max-width: 600px)"
          :srcset="visualizationFile(threadId, artifactId, 'mobile.png')"
        />
        <img
          :src="
            visualizationFile(
              threadId,
              artifactId,
              artifact.viewports.includes('desktop') ? 'desktop.png' : 'mobile.png',
            )
          "
          alt="已审核图表预览"
          @error="error = '图表图片不可访问，请刷新任务或检查来源权限。'"
        />
      </picture>
      <nav aria-label="图表下载">
        <a
          v-for="viewport in artifact.viewports"
          :key="viewport"
          :href="visualizationFile(threadId, artifactId, viewport + '.png')"
          :download="viewport + '.png'"
          >下载{{ viewport === 'desktop' ? '宽屏' : '窄屏' }} PNG</a
        >
        <a :href="visualizationFile(threadId, artifactId, 'source.js')" download="source.js"
          >下载绘图代码</a
        >
      </nav>
      <details>
        <summary>查看图表来源与版本</summary>
        <p>图表引用：{{ artifact.artifact_id }}</p>
        <p v-if="artifact.parent_artifact_id">上一版本：{{ artifact.parent_artifact_id }}</p>
        <p>数据摘要：{{ artifact.snapshot_sha256 }}</p>
        <ul>
          <li v-for="source in artifact.data_refs" :key="source.source_id">
            {{ source.kind }} · {{ source.source_id }} · {{ source.run_id }}
          </li>
        </ul>
        <p>审核记录：{{ artifact.review_id }}；视觉审核不替代数值核验。</p>
      </details>
      <form
        v-if="!readOnly"
        @submit.prevent="feedback.trim() && emit('revise', artifactId, feedback.trim())"
      >
        <label
          >修改这张图<textarea
            v-model="feedback"
            aria-label="图表修改要求"
            maxlength="4000"
            placeholder="例如：将图例移到下方，使用蓝色系，保留当前数据范围"
          />
        </label>
        <button :disabled="!feedback.trim()" type="submit">生成新版本</button>
      </form>
    </template>
  </section>
</template>

<style scoped>
.visualization-artifact {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 16px;
  min-width: 0;
  margin: 12px 0;
}
img {
  display: block;
  width: 100%;
  height: auto;
  background: white;
}
p,
li {
  overflow-wrap: anywhere;
}
.intent {
  white-space: pre-wrap;
}
nav {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
}
label,
textarea {
  display: block;
  width: 100%;
}
textarea {
  box-sizing: border-box;
  margin: 8px 0;
  min-height: 72px;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  padding: 10px;
}
button {
  padding: 8px 12px;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
