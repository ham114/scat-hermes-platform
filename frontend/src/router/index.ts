import { createRouter, createWebHistory } from 'vue-router'

const HermesAssistantView = () => import('@/views/HermesAssistantView.vue')
const EvaluationView = () => import('@/views/EvaluationView.vue')
const FlightDetailEntryView = () => import('@/views/FlightDetailEntryView.vue')
const FlightSearchView = () => import('@/views/FlightSearchView.vue')
const FoundationView = () => import('@/views/FoundationView.vue')
const NotFoundView = () => import('@/views/NotFoundView.vue')
const OverviewView = () => import('@/views/OverviewView.vue')
const PortalView = () => import('@/views/PortalView.vue')
const VisualizationEntryView = () => import('@/views/VisualizationEntryView.vue')

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    {
      path: '/memory',
      name: 'memory',
      component: () => import('@/views/MemoryView.vue'),
      meta: { title: '记忆' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/AgentSettingsView.vue'),
      meta: { title: 'Agent 设置' },
    },
    {
      path: '/skills',
      name: 'skills',
      component: () => import('@/views/SkillsView.vue'),
      meta: { title: '技能管理' },
    },
    {
      path: '/data',
      name: 'data',
      component: () => import('@/views/DataBrowserView.vue'),
      meta: { title: '数据浏览' },
    },
    {
      path: '/history',
      name: 'history',
      component: () => import('@/views/HistoryView.vue'),
      meta: { title: '历史会话' },
    },
    {
      path: '/',
      name: 'portal',
      component: PortalView,
      meta: { title: '对话' },
    },
    {
      path: '/foundation',
      name: 'foundation',
      component: FoundationView,
      meta: { title: '工程基础' },
    },
    {
      path: '/overview',
      name: 'overview',
      component: OverviewView,
      meta: { title: '总览工作台', milestone: 'M3-V1' },
    },
    {
      path: '/flights',
      name: 'flights',
      component: FlightSearchView,
      meta: { title: '航班检索', milestone: 'M3-V2' },
    },
    {
      path: '/flights/:batch/:externalId',
      name: 'flight-detail',
      component: FlightDetailEntryView,
      meta: { title: '单航班分析', milestone: 'M3-V3' },
    },
    {
      path: '/visualizations/:artifactId/3d',
      name: 'visualization-3d',
      component: VisualizationEntryView,
      meta: { title: '轨迹预测结果', milestone: 'M3-S2' },
    },
    {
      path: '/evaluation',
      name: 'evaluation',
      component: EvaluationView,
      meta: { title: '预测评估', milestone: 'M5-08' },
    },
    {
      path: '/assistant',
      name: 'assistant',
      component: HermesAssistantView,
      meta: { title: '对话' },
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView },
  ],
})

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? '页面未找到')} · Hermes`
})
