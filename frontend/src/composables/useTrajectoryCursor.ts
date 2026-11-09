import { inject, type InjectionKey } from 'vue'
import { useTimeCursorStore } from '@/stores/timeCursor'
export const trajectoryCursorKey: InjectionKey<ReturnType<typeof useTimeCursorStore>> =
  Symbol('trajectory-cursor')
export function useTrajectoryCursor() {
  return inject(trajectoryCursorKey, null) ?? useTimeCursorStore()
}
