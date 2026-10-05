<template>
  <div>
    <h1>设置</h1>
    <div class="combo-box">
      <h3>临期预警天数 warn_days</h3>
      <input type="number" min="0" v-model.number="warnDays" style="max-width:140px" />
      <button @click="save">保存</button>
      <span class="ok" v-if="saved">已保存，顶条预警按新天数现算；已确认组合的扣减快照不会被改写。</span>
    </div>
    <pre>{{ s }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const s = ref('')
const warnDays = ref(3)
const saved = ref(false)
onMounted(async () => {
  const cur = await api('/settings')
  s.value = JSON.stringify(cur, null, 2)
  warnDays.value = Number(cur.warn_days)
})
async function save() {
  saved.value = false
  await api('/settings', { method: 'PATCH', body: JSON.stringify({ warn_days: warnDays.value }) })
  s.value = JSON.stringify(await api('/settings'), null, 2)
  saved.value = true
}
</script>
