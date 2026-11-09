<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => String(route.meta.title ?? '待开发模块'))
const milestone = computed(() => String(route.meta.milestone ?? '后续里程碑'))
</script>

<template>
  <section class="page page-centered">
    <div class="placeholder-code">{{ milestone }}</div>
    <h1>{{ title }}</h1>
    <p>路由与页面边界已经预留，业务能力将在对应里程碑中实现。</p>
    <router-link class="text-link" to="/foundation">返回工程基础</router-link>
  </section>
</template>
