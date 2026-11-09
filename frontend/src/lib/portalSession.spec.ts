import { afterEach, describe, expect, it } from 'vitest'
import {
  acceptPortalPrincipal,
  conversationStorageKey,
  canManagePortal,
  portalPrincipal,
  portalStatus,
} from './portalSession'

afterEach(() => {
  sessionStorage.clear()
  portalPrincipal.value = undefined
  portalStatus.value = 'loading'
})
describe('portal browser identity', () => {
  it('keeps prior HYN history but never hands it to ZYJ', () => {
    sessionStorage.setItem('scat-hermes-conversation-v1', 'PRIVATE-HYN')
    acceptPortalPrincipal({ username: 'HYN', owner_id: 'scat-researcher', role: 'user' })
    expect(sessionStorage.getItem(conversationStorageKey())).toBe('PRIVATE-HYN')
    expect(canManagePortal.value).toBe(false)
    acceptPortalPrincipal({ username: 'ZYJ', owner_id: 'scat-zyj', role: 'user' })
    expect(sessionStorage.getItem(conversationStorageKey())).toBe(null)
    expect(Object.values(sessionStorage)).not.toContain('PRIVATE-HYN')
  })
  it('clears unscoped legacy history on the first ZYJ login and allows only the registered admin role', () => {
    sessionStorage.setItem('scat-hermes-conversation-v1', 'PRIVATE-HYN')
    acceptPortalPrincipal({ username: 'ZYJ', owner_id: 'scat-zyj', role: 'user' })
    expect(sessionStorage.length).toBe(0)
    acceptPortalPrincipal({ username: 'admin', owner_id: 'scat-admin', role: 'admin' })
    expect(canManagePortal.value).toBe(true)
  })
})
