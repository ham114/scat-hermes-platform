<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getWorkflowCatalog } from '@/api/hermes'
import type { WorkflowCatalog } from '@/api/hermes'
import DirectForecastForm from './DirectForecastForm.vue'
defineProps<{ disabled?: boolean; readOnly?: boolean }>()
const catalog = ref<WorkflowCatalog>()
const emit = defineEmits<{
  example: [message: string]
  prepare: [proposal: Record<string, unknown>, budget: Record<string, number>]
}>()
const unavailable = ref(false)
const labels: Record<string, string> = {
  scat_data_steps: '固定样本',
  scat_materialization_steps: '数据物化',
  scat_experiment_steps: '模型与实验',
  scat_evaluation_steps: '评估诊断',
  scat_report_steps: '报告',
}
onMounted(async () => {
  try {
    const value = await getWorkflowCatalog()
    if (
      !value ||
      typeof value.enabled !== 'boolean' ||
      !Array.isArray(value.model_ids) ||
      !value.groups ||
      typeof value.groups !== 'object'
    )
      throw new Error('Invalid workflow catalog')
    catalog.value = value
  } catch {
    unavailable.value = true
  }
})
</script>
<template>
  <section class="panel workflow-catalog" aria-label="技能与工具组">
    <details>
      <summary>技能与工具组 · {{ catalog?.enabled ? '复杂流程已启用' : '能力目录' }}</summary>
      <p v-if="unavailable">暂时无法读取服务端能力目录。</p>
      <template v-if="catalog">
        <button
          v-if="catalog.enabled && catalog.synthetic"
          type="button"
          class="secondary-link"
          @click="
            emit(
              'example',
              '请加载 scat-model-comparison 技能，准备 SYNTH-S5 批次 SYNTH-S5-001、SYNTH-S5-002 的 constant-velocity-heading 与 kalman-cv 双模型比较，批准 phase_segments 物化，missing_phase=build。先展示方案，等待我在页面确认。',
            )
          "
        >
          填入合成双模型比较示例
        </button>
        <p>
          {{
            catalog.synthetic ? '当前为合成数据展示配置。' : '业务数据仅在服务器处理。'
          }}助手准备方案，你核对后确认，由独立后台 Agent 执行。
        </p>
        <div class="groups">
          <article v-for="(tools, group) in catalog.groups" :key="group">
            <h3>{{ labels[group] ?? group }}</h3>
            <code>{{ group }}</code>
            <ul>
              <li v-for="tool in tools" :key="tool">{{ tool }}</li>
            </ul>
          </article>
        </div>
        <p>
          已登记模型：{{ catalog.model_ids.join(' / ') }}。生产 GPU：{{
            catalog.production_gpu ? '已启用' : '未启用'
          }}。
        </p>
        <p v-if="!catalog.enabled">当前配置未启用复杂流程执行。</p>
      </template>
    </details>
    <DirectForecastForm
      v-if="catalog?.enabled && catalog.models?.length"
      :models="catalog.models"
      :disabled="disabled"
      :read-only="readOnly"
      @prepare="(proposal, budget) => emit('prepare', proposal, budget)"
    />
  </section>
</template>
<style scoped>
.workflow-catalog {
  padding: 16px;
  margin: 16px 0;
}
.groups {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
}
.groups article {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px;
  overflow-wrap: anywhere;
}
code,
li {
  font-size: 12px;
}
</style>
