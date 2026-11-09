import { createApp } from 'vue'
import {
  ElAlert,
  ElButton,
  ElEmpty,
  ElInput,
  ElOption,
  ElSelect,
  ElSkeleton,
  ElSlider,
} from 'element-plus'
import { createPinia } from 'pinia'

import 'element-plus/es/components/base/style/css'
import 'element-plus/es/components/alert/style/css'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/empty/style/css'
import 'element-plus/es/components/input/style/css'
import 'element-plus/es/components/option/style/css'
import 'element-plus/es/components/select/style/css'
import 'element-plus/es/components/skeleton/style/css'
import 'element-plus/es/components/skeleton-item/style/css'
import 'element-plus/es/components/slider/style/css'
import './style.css'
import './workspace.css'
import App from './App.vue'
import { router } from './router'

createApp(App)
  .use(createPinia())
  .use(router)
  .component('ElAlert', ElAlert)
  .component('ElButton', ElButton)
  .component('ElEmpty', ElEmpty)
  .component('ElInput', ElInput)
  .component('ElOption', ElOption)
  .component('ElSelect', ElSelect)
  .component('ElSkeleton', ElSkeleton)
  .component('ElSlider', ElSlider)
  .mount('#app')
