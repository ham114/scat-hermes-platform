<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const batch = computed(() => String(route.params.batch ?? ''))
const externalId = computed(() => String(route.params.externalId ?? ''))
const backTarget = computed(() => ({ name: 'flights', query: { ...route.query } }))
</script>

<template>
  <section class="page flight-entry-page">
    <router-link class="back-link" :to="backTarget">← 返回航班检索</router-link>
    <article class="panel flight-entry-card">
      <span class="eyebrow">M3-V3 · Analysis entry</span>
      <h1>{{ externalId }}</h1>
      <p>单航班分析入口已建立，检索条件与页码会在返回时完整恢复。</p>
      <dl>
        <div>
          <dt>数据批次</dt>
          <dd>{{ batch }}</dd>
        </div>
        <div>
          <dt>来源航班编号</dt>
          <dd>{{ externalId }}</dd>
        </div>
      </dl>
      <div class="next-stage-note">轨迹、FPL、预测快照与时序联动将在 M3-V3 接入。</div>
    </article>
  </section>
</template>
