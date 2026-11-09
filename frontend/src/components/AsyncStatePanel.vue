<script setup lang="ts">
import { computed } from 'vue'

import { ScatApiError, toDisplayMessage } from '@/api/errors'

const props = withDefaults(
  defineProps<{
    state: 'idle' | 'loading' | 'empty' | 'error'
    error?: unknown
    emptyDescription?: string
    loadingDescription?: string
  }>(),
  {
    error: undefined,
    emptyDescription: '暂无符合条件的数据。',
    loadingDescription: '正在等待 SCAT 服务响应…',
  },
)

defineEmits<{ retry: [] }>()

const requestId = computed(() =>
  props.error instanceof ScatApiError ? props.error.requestId : undefined,
)
</script>

<template>
  <div class="async-state" :data-state="state">
    <template v-if="state === 'loading'">
      <el-skeleton :rows="3" animated />
      <p class="state-caption">{{ loadingDescription }}</p>
    </template>

    <el-empty v-else-if="state === 'empty'" :description="emptyDescription" :image-size="72" />

    <template v-else-if="state === 'error'">
      <el-alert :title="toDisplayMessage(error)" type="error" :closable="false" show-icon />
      <div class="state-actions">
        <span v-if="requestId" class="request-id">Request ID: {{ requestId }}</span>
        <el-button type="primary" plain @click="$emit('retry')">重新检查</el-button>
      </div>
    </template>

    <div v-else class="idle-state">
      <span class="radar-mark" aria-hidden="true"></span>
      <div>
        <strong>等待接口检查</strong>
        <p>点击按钮后，将通过同源代理读取一个批次并验证类型化客户端。</p>
      </div>
    </div>
  </div>
</template>
