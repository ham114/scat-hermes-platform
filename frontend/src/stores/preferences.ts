import { defineStore } from 'pinia'

export type ColorTheme = 'dark' | 'light'

function preferredTheme(): ColorTheme {
  const saved = localStorage.getItem('scat-theme')
  if (saved === 'dark' || saved === 'light') return saved
  return 'light'
}

export const usePreferencesStore = defineStore('preferences', {
  state: () => ({ theme: 'light' as ColorTheme }),
  actions: {
    initialize() {
      this.apply(preferredTheme())
    },
    toggleTheme() {
      this.apply(this.theme === 'dark' ? 'light' : 'dark')
    },
    apply(theme: ColorTheme) {
      this.theme = theme
      document.documentElement.dataset.theme = theme
      localStorage.setItem('scat-theme', theme)
    },
  },
})
