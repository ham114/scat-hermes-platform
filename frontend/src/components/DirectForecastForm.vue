<script setup lang="ts">
import { computed, ref } from 'vue'
import type { WorkflowModel } from '@/api/hermes'
const props = defineProps<{ models: WorkflowModel[]; disabled?: boolean; readOnly?: boolean }>()
const emit = defineEmits<{
  prepare: [proposal: Record<string, unknown>, budget: Record<string, number>]
}>()
const batch = ref(''),
  flight = ref(''),
  modelId = ref(''),
  points = ref(200),
  horizons = ref('10,30,60'),
  units = ref(1000000),
  error = ref('')
const model = computed(() => props.models.find((m) => m.model_id === modelId.value))
function prepare() {
  if (props.disabled || props.readOnly) return
  error.value = ''
  const selected = model.value,
    times = horizons.value
      .trim()
      .split(/[,，\s]+/)
      .map(Number)
  if (!batch.value.trim() || !flight.value.trim() || !selected) {
    error.value = '请填写批次、航班并选择已登记模型。'
    return
  }
  if (
    !Number.isInteger(points.value) ||
    points.value < 3 ||
    points.value > (selected.constraints?.max_observation_points ?? 2000)
  ) {
    error.value = '观测点数必须是登记模型范围内的整数。'
    return
  }
  if (
    !times.length ||
    times.length > 6 ||
    times.some(
      (h, i) =>
        !Number.isFinite(h) ||
        h <= 0 ||
        h > (selected.constraints?.max_horizon_s ?? 1800) ||
        (i > 0 && h <= times[i - 1]!) ||
        h % (selected.constraints?.forecast_horizon_multiple_s ?? 1),
    )
  ) {
    error.value = '预测时距须从小到大、无重复，并符合模型时距和步长限制。'
    return
  }
  if (!Number.isInteger(units.value) || units.value < 1 || units.value > 1000000) {
    error.value = '后台额度只能在1到1000000之间收紧。'
    return
  }
  emit(
    'prepare',
    {
      kind: 'forecast',
      batch: batch.value.trim(),
      external_ids: [flight.value.trim()],
      max_flights: 1,
      models: [{ model: selected.model_id, version: selected.version }],
      observation_last_n: points.value,
      forecast_origin: 'last_observation',
      horizons_s: times,
      min_observed_s: selected.constraints?.min_observed_s ?? 60,
      max_cutoffs_per_flight: 1,
    },
    { max_llm_tokens: units.value },
  )
}
</script>
<template>
  <details class="direct-forecast">
    <summary>直接准备预测方案</summary>
    <p>填写明确参数可直接提交，无需对话规划。这里只准备方案，核对后仍须另行确认执行。</p>
    <form aria-label="直接预测参数" @submit.prevent="prepare">
      <fieldset :disabled="disabled || readOnly">
        <label
          >数据批次<input v-model="batch" aria-label="数据批次" required maxlength="100"
        /></label>
        <label
          >航班编号<input v-model="flight" aria-label="航班编号" required maxlength="100"
        /></label>
        <label
          >预测模型<select v-model="modelId" aria-label="预测模型" required>
            <option value="">请选择模型</option>
            <option v-for="item in models" :key="item.model_id" :value="item.model_id">
              {{ item.model_id }}:{{ item.version }} · {{ item.execution }}
            </option>
          </select></label
        >
        <label
          >最后观测点数<input
            v-model.number="points"
            aria-label="最后观测点数"
            type="number"
            min="3"
            :max="model?.constraints?.max_observation_points ?? 2000"
            step="1"
            required
        /></label>
        <label
          >未来时距（秒）<input
            v-model="horizons"
            aria-label="未来时距（秒）"
            maxlength="100"
            required
        /></label>
        <details>
          <summary>后台运行额度</summary>
          <label
            >保守计量上限<input
              v-model.number="units"
              aria-label="保守计量上限"
              type="number"
              min="1"
              max="1000000"
              step="1"
              required
          /></label>
          <p>额度只约束本项后台任务，可降低；保守计量单位不是费用。</p>
        </details>
        <button type="submit" class="secondary-link">准备预测方案</button>
      </fieldset>
      <p v-if="error" role="alert">{{ error }}</p>
    </form>
  </details>
</template>
<style scoped>
.direct-forecast {
  margin-top: 16px;
}
fieldset {
  border: 0;
  padding: 12px 0;
  display: grid;
  gap: 12px;
}
label {
  display: grid;
  gap: 6px;
}
input,
select {
  box-sizing: border-box;
  max-width: 100%;
  width: 100%;
  padding: 8px;
  color: var(--text);
  background: var(--surface-raised);
  border: 1px solid var(--line);
  border-radius: 6px;
}
</style>
