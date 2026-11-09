<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import WorkspaceIcon from '@/components/WorkspaceIcon.vue'
import { useFilterQuerySync } from '@/composables/useFilterQuerySync'
import { usePreferencesStore } from '@/stores/preferences'
import {
  portalPrincipal,
  portalStatus,
  canManagePortal,
  loadPortalSession,
} from '@/lib/portalSession'

const route = useRoute()
const preferences = usePreferencesStore()
const menuOpen = ref(false)
const pageTitle = computed(() => String(route.meta.title ?? '工作台'))
const navigation = [
  { to: '/', icon: 'chat', label: '对话', active: ['portal', 'assistant'] },
  { to: '/memory', icon: 'spark', label: '记忆', active: ['memory'] },
  { to: '/settings', icon: 'flow', label: 'Agent 设置', active: ['settings'] },
  { to: '/skills', icon: 'book', label: '技能管理', active: ['skills'] },
  { to: '/history', icon: 'clock', label: '历史会话', active: ['history'] },
  {
    to: '/data',
    icon: 'data',
    label: '数据浏览',
    active: ['data', 'overview', 'flights', 'flight-detail', 'evaluation', 'visualization-3d'],
  },
]
const visibleNavigation = computed(() =>
  navigation.filter((item) => !['/settings', '/skills'].includes(item.to) || canManagePortal.value),
)
const restricted = computed(
  () => ['settings', 'skills'].includes(String(route.name)) && !canManagePortal.value,
)
useFilterQuerySync()
watch(
  () => route.fullPath,
  () => {
    menuOpen.value = false
  },
)
watch(menuOpen, (open) => {
  void nextTick(() => {
    const target = open
      ? document.querySelector<HTMLElement>('#workspace-navigation nav a')
      : document.getElementById('workspace-menu')
    target?.focus({ preventScroll: true })
  })
})
onMounted(() => {
  preferences.initialize()
  void loadPortalSession()
})
</script>
<template>
  <div class="app-shell workspace-shell" @keydown.esc="menuOpen = false">
    <a class="skip-link" href="#workspace-main">跳到主要内容</a>
    <button
      v-if="menuOpen"
      class="workspace-scrim"
      aria-label="关闭导航"
      @click="menuOpen = false"
    />
    <aside
      id="workspace-navigation"
      class="workspace-sidebar"
      :class="{ open: menuOpen }"
      @keydown.esc="menuOpen = false"
    >
      <router-link class="workspace-brand" to="/" aria-label="Hermes 首页">
        <span class="workspace-mark"><WorkspaceIcon name="spark" :size="23" /></span>
        <strong>Hermes<span>工作台</span></strong>
      </router-link>
      <div class="workspace-nav-caption">工作空间</div>
      <nav aria-label="主导航">
        <router-link
          v-for="item in visibleNavigation"
          :key="item.to"
          :to="item.to"
          class="workspace-nav-item"
          :class="{ selected: item.active.includes(String(route.name)) }"
          :aria-current="item.active.includes(String(route.name)) ? 'page' : undefined"
        >
          <WorkspaceIcon :name="item.icon" :size="19" /><span>{{ item.label }}</span>
          <span v-if="item.active.includes(String(route.name))" class="workspace-nav-dot" />
        </router-link>
      </nav>
      <div class="workspace-sidebar-bottom">
        <button
          class="workspace-theme"
          :aria-label="preferences.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'"
          @click="preferences.toggleTheme()"
        >
          <WorkspaceIcon :name="preferences.theme === 'dark' ? 'sun' : 'moon'" :size="18" />
          <span>{{ preferences.theme === 'dark' ? '浅色模式' : '深色模式' }}</span>
        </button>
        <div class="workspace-account">
          <span class="workspace-avatar">我</span
          ><span>{{
            portalPrincipal
              ? portalPrincipal.username +
                (portalPrincipal.role === 'admin' ? ' · 管理员' : ' · 个人空间')
              : '个人空间'
          }}</span>
        </div>
      </div>
    </aside>
    <div class="workspace-main-column">
      <header class="workspace-topbar">
        <div class="workspace-breadcrumb">
          <button
            id="workspace-menu"
            class="workspace-icon-button workspace-menu"
            aria-label="打开导航"
            aria-controls="workspace-navigation"
            :aria-expanded="menuOpen"
            @click="menuOpen = !menuOpen"
          >
            <WorkspaceIcon name="menu" />
          </button>
          <span>工作空间</span><span class="workspace-slash">/</span
          ><strong>{{ pageTitle }}</strong>
        </div>
        <span class="workspace-topbar-wordmark">HERMES</span>
      </header>
      <main id="workspace-main" tabindex="-1">
        <p v-if="portalStatus === 'loading'" role="status">正在核验登录身份…</p>
        <p v-else-if="portalStatus === 'failed'" role="alert">
          无法核验当前登录身份，请刷新页面后重试。
        </p>
        <p v-else-if="restricted" role="alert">此页面仅管理员可访问。</p>
        <router-view v-else :key="portalPrincipal?.owner_id ?? 'legacy'" />
      </main>
    </div>
  </div>
</template>
