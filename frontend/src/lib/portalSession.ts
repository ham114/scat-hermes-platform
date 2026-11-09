import { computed, ref } from 'vue'

export interface PortalPrincipal {
  username: string
  owner_id: string
  role: 'user' | 'admin'
}
export const portalPrincipal = ref<PortalPrincipal>()
export const portalStatus = ref<'loading' | 'managed' | 'unmanaged' | 'failed'>('loading')
export const canManagePortal = computed(
  () => portalStatus.value === 'unmanaged' || portalPrincipal.value?.role === 'admin',
)
export function conversationStorageKey() {
  return (
    'scat-hermes-conversation-v1' +
    (portalPrincipal.value ? ':' + portalPrincipal.value.owner_id : '')
  )
}
export function acceptPortalPrincipal(value: PortalPrincipal) {
  if (
    !value ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$/.test(value.owner_id) ||
    !['user', 'admin'].includes(value.role) ||
    !/^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$/.test(value.username)
  )
    throw new Error('Invalid portal identity')
  portalPrincipal.value = value
  const prefix = 'scat-hermes-conversation-v1'
  try {
    const key = conversationStorageKey(),
      legacy = sessionStorage.getItem(prefix)
    if (
      legacy &&
      value.username === 'HYN' &&
      value.owner_id === 'scat-researcher' &&
      !sessionStorage.getItem(key)
    )
      sessionStorage.setItem(key, legacy)
    for (const name of Object.keys(sessionStorage))
      if (name.startsWith(prefix) && name !== key) sessionStorage.removeItem(name)
  } catch {
    /* Storage is optional; no identity is inferred from it. */
  }
  portalStatus.value = 'managed'
}
export async function loadPortalSession() {
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const response = await fetch('/portal-session', {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
    })
    if (!response.ok) throw new Error('Portal identity unavailable')
    acceptPortalPrincipal(await response.json())
  } catch {
    portalPrincipal.value = undefined
    portalStatus.value = ['127.0.0.1', 'localhost'].includes(window.location.hostname)
      ? 'unmanaged'
      : 'failed'
  } finally {
    clearTimeout(timeout)
  }
}
