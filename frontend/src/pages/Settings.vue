<template>
  <div>
    <h1>设置</h1>
    <label>临期预警天数 warn_days</label>
    <input type="number" min="0" v-model.number="warn_days" />
    <button @click="save">保存</button>
    <p class="muted">顶条按新天数现算；已确认组合的扣减快照保持确认时的天数，不回改。</p>
    <p v-if="msg" class="muted">{{ msg }}</p>
    <pre>{{ s }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const s = ref('')
const warn_days = ref(3)
const msg = ref('')
async function load() {
  const j = await api('/settings')
  s.value = JSON.stringify(j, null, 2)
  warn_days.value = Number(j.warn_days)
}
async function save() {
  try {
    const j = await api('/settings', { method: 'PUT', body: JSON.stringify({ warn_days: warn_days.value }) })
    msg.value = '已保存 warn_days=' + j.warn_days
    await load()
  } catch (e) { msg.value = e.message }
}
onMounted(load)
</script>
