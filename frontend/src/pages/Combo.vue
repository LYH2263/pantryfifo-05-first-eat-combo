<template>
  <div>
    <h1>先吃组合</h1>
    <p class="muted">按当前在架正余量拟扣 · 预览不改库存 · 确认整组原子写入：一品不够则整组不写</p>

    <div v-for="c in candidates" :key="c.id" class="combo-row">
      <span class="combo-name">{{ c.name }}（可组合余量 {{ c.avail }} {{ c.unit }}）</span>
      <input type="number" min="0" :max="c.avail" step="any" v-model.number="want[c.id]" placeholder="0" />
    </div>
    <p v-if="excluded.length" class="muted">未进组合（余量非正或数据脏）：{{ excluded.join('、') }}</p>

    <button @click="preview" :disabled="!demands.length">预览拟扣</button>
    <button @click="confirm" :disabled="!plan || !plan.ok || confirmed || !demands.length" style="margin-left:8px">确认组合</button>

    <div v-if="plan">
      <h2>{{ confirmed ? '已确认快照' : '拟扣预览（未改库存）' }}</h2>
      <div v-for="it in plan.items" :key="it.item_id">
        <b>{{ nameOf(it.item_id) }} ×{{ it.qty }}</b>
        <span v-if="it.ok">
          <span v-for="d in it.deductions" :key="d.lot_id" class="lot">批#{{ d.lot_id }} 扣 {{ d.take }} · {{ d.expiry }}</span>
        </span>
        <span v-else class="short">缺口 {{ it.short }}（{{ it.reason }}）</span>
      </div>
      <p v-if="!plan.ok" class="short">有品不够：确认将整组回滚，不写任何一品</p>
    </div>

    <p v-if="conflict" class="short">{{ conflict }}</p>
    <p v-if="doneId">
      已写入消费记录 #{{ doneId }} ·
      <router-link to="/">到全层核对余量 →</router-link>
    </p>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'

const items = ref([])
const lots = ref([])
const want = ref({})
const plan = ref(null)
const confirmed = ref(false)
const conflict = ref('')
const doneId = ref(0)

const candidates = computed(() =>
  items.value
    .map(i => ({ ...i, avail: lots.value
      .filter(l => l.item_id === i.id && Number(l.qty_remain) > 0 && l.data_quality === 'clean')
      .reduce((s, l) => s + Number(l.qty_remain), 0) }))
    .filter(i => i.avail > 0))
const excluded = computed(() =>
  items.value.filter(i => !candidates.value.some(c => c.id === i.id)).map(i => i.name))
const demands = computed(() =>
  Object.entries(want.value)
    .map(([item_id, qty]) => ({ item_id: Number(item_id), qty: Number(qty) }))
    .filter(d => d.qty > 0))

function nameOf(id) { return (items.value.find(i => i.id === id) || {}).name || '#' + id }

async function load() {
  ;[items.value, lots.value] = await Promise.all([api('/items'), api('/fridge')])
}

async function preview() {
  conflict.value = ''; confirmed.value = false; doneId.value = 0
  try {
    plan.value = await api('/combo/preview', { method: 'POST', body: JSON.stringify({ items: demands.value }) })
  } catch (e) { plan.value = null; conflict.value = e.message }
}

async function confirm() {
  conflict.value = ''
  try {
    const snap = await api('/combo/confirm', { method: 'POST', body: JSON.stringify({ items: demands.value }) })
    plan.value = snap
    confirmed.value = true
    doneId.value = snap.consumption_id
    want.value = {}
    await load() // 确认后刷新余量，候选区与全层一致
  } catch (e) {
    confirmed.value = false
    // 409: 并发旁路（单品消费/下架）已抢先，整组未写入任何扣减
    const detail = e.body && e.body.detail
    if (e.status === 409 && detail && detail.items) {
      plan.value = detail
      conflict.value = '旁路已抢先或余量不足：整组未写入，请按最新余量重新预览'
      await load()
    } else {
      conflict.value = e.message
    }
  }
}

onMounted(load)
</script>

<style scoped>
.combo-row { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
.combo-name { min-width: 220px; }
.combo-row input { max-width: 120px; margin: 0; }
.short { color: var(--alert); }
h2 { font-size: 16px; margin: 14px 0 6px; }
</style>
