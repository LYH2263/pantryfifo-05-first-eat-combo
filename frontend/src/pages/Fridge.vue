<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页 · 多品拟扣走「先吃组合」</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <span v-for="x in by(L)" :key="x.id" class="lot">
          {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}<em v-if="x.data_quality !== 'clean'" class="dirty">脏</em>
        </span>
        <p class="muted" v-if="subtotals(L).length">余量小计：{{ subtotals(L).map(([n, q]) => n + ' 余 ' + q).join(' · ') }}</p>
      </section>
    </div>
    <button style="margin-top:12px" @click="sweep">过期下架</button>
    <button style="margin-top:12px;margin-left:8px" @click="load">刷新核对</button>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
function by(L) { return rows.value.filter(r => r.layer === L) }
// 分品正余量小计：组合确认后在这里核对这些品还剩多少
function subtotals(L) {
  const m = {}
  for (const r of rows.value) {
    const q = Number(r.qty_remain)
    if (r.layer === L && q > 0) m[r.name] = (m[r.name] || 0) + q
  }
  return Object.entries(m)
}
async function load() { rows.value = await api('/fridge') }
async function sweep() { await api('/expire-sweep', { method: 'POST', body: '{}' }); await load() }
onMounted(load)
</script>
<style scoped>
.dirty { color: var(--alert); font-style: normal; margin-left: 4px; }
</style>
