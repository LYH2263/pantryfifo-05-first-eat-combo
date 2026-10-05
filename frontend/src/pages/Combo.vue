<template>
  <div>
    <h1>先吃组合</h1>
    <p class="muted">
      按当前在架正余量做多品 FEFO 拟扣预览（预览不动批次）。确认与单品消费 / 过期下架互斥：
      同一批只会留下一种结果。dirty 批次不参与扣减。
    </p>

    <section class="combo-box">
      <h3>1. 选品（仅在架 · 正余量 · clean）</h3>
      <select v-model.number="pickId">
        <option v-for="i in itemOptions" :key="i.id" :value="i.id">
          {{ i.name }}（可吃 {{ fmt(i.avail) }}{{ i.unit }}，{{ i.layerLabel }}）
        </option>
      </select>
      <input type="number" min="0" step="any" v-model.number="pickQty" />
      <button @click="addLine" :disabled="!pickAvailable">加入组合</button>
      <p class="muted" v-if="excludedDirty.length">
        不参与：<span v-for="x in excludedDirty" :key="x.id">{{ x.name }} 批次#{{ x.id }}（dirty ×{{ x.qty_remain }}） </span>
      </p>
    </section>

    <section class="combo-box" v-if="lines.length">
      <h3>2. 组合清单</h3>
      <table class="combo-table">
        <tr v-for="l in lines" :key="l.item_id">
          <td>{{ nameOf(l.item_id) }}</td>
          <td>要吃 <input type="number" min="0" step="any" v-model.number="l.qty" /></td>
          <td>在架可吃 {{ fmt(availOf(l.item_id)) }}{{ unitOf(l.item_id) }}</td>
          <td><button class="ghost" @click="removeLine(l.item_id)">移除</button></td>
        </tr>
      </table>
      <label class="mode"><input type="radio" :value="false" v-model="allowPartial" /> 整组：有一品不够，整组一个批次都不写</label>
      <label class="mode"><input type="radio" :value="true" v-model="allowPartial" /> 部分品：够的品成功，短的品整品跳过</label>
    </section>

    <section v-if="lines.length">
      <button @click="preview" :disabled="loading">拟扣预览</button>
      <button class="primary" @click="confirm" :disabled="loading || !previewData">确认组合</button>
      <span class="muted" v-if="!previewData">请先预览再确认</span>
    </section>

    <section class="combo-box" v-if="previewError">
      <h3 class="err">预览失败</h3>
      <pre>{{ previewError }}</pre>
    </section>

    <section class="combo-box" v-if="previewData">
      <h3>3. 拟扣预览<span class="muted">（lots 未改动）</span></h3>
      <p v-if="!previewData.ok" class="warn">整组无法满足，确认将整体拒绝（不写任何扣减）</p>
      <p v-else-if="previewData.reason === 'partial'" class="warn">部分品模式：下列短品将整品跳过</p>
      <table class="combo-table" v-if="groupedPreview.length">
        <tr><th>品</th><th>批次 FEFO</th><th>到期</th><th>拟扣</th><th>扣后</th></tr>
        <template v-for="g in groupedPreview" :key="g.item_id">
          <tr v-for="(d, idx) in g.deductions" :key="d.lot_id">
            <td v-if="idx === 0">{{ g.name }} ×{{ fmt(g.qty) }}</td>
            <td v-else></td>
            <td>#{{ d.lot_id }}</td>
            <td>{{ d.expiry }}</td>
            <td>{{ fmt(d.take) }}</td>
            <td>{{ fmt(lotRemain(d.lot_id) - d.take) }}</td>
          </tr>
        </template>
      </table>
      <p v-for="s in previewData.short_items" :key="s.item_id" class="warn">
        ⚠ {{ s.name }} 缺 {{ fmt(s.short) }}（要 {{ fmt(s.qty) }}）
      </p>
    </section>

    <section class="combo-box" v-if="confirmError">
      <h3 class="err">确认未写入</h3>
      <pre>{{ confirmError }}</pre>
      <p class="muted">可能有单品消费 / 下架刚动了同一批，请重新预览再确认。</p>
    </section>

    <section class="combo-box" v-if="result">
      <h3>4. 已确认 <span class="muted">组合 #{{ result.combo_id }} · 快照 warn_days={{ result.warn_days }}</span></h3>
      <p v-if="result.reason === 'partial'" class="warn">部分品成功；短品：
        <span v-for="s in result.short_items" :key="s.item_id">{{ s.name }}(缺{{ fmt(s.short) }}) </span>
      </p>
      <button @click="crossCheck" :disabled="checking">全层核对这些品的余量</button>
      <div v-if="checkRows.length">
        <table class="combo-table">
          <tr><th>层</th><th>品</th><th>批次</th><th>回包 take</th><th>回包余量</th><th>全层现余量</th><th></th></tr>
          <tr v-for="r in checkRows" :key="r.lot_id" :class="{ mismatch: !r.match }">
            <td><router-link :to="'/layer/' + r.layer">{{ layerLabel(r.layer) }}</router-link></td>
            <td>{{ r.name }}</td>
            <td>#{{ r.lot_id }}<span v-if="r.dirty" class="badge-dirty">dirty</span></td>
            <td>{{ fmt(r.take) }}</td>
            <td>{{ fmt(r.respRemain) }}{{ r.respStatus === 'consumed' ? '（已离架）' : '' }}</td>
            <td>{{ r.actual === null ? '已离架（0）' : fmt(r.actual) }}</td>
            <td>{{ r.match ? '✓ 一致' : '✗ 不一致' }}</td>
          </tr>
        </table>
        <p v-if="allMatch" class="ok">全部批次：层页余量与回包 take 对得上。</p>
        <p v-else class="warn">存在不一致<span v-if="someChangedAfter">（部分批次在确认后又被单品消费/下架改动）</span>，请重新预览核对。</p>
      </div>
    </section>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'

const rows = ref([])
const items = ref([])
const lines = ref([])
const pickId = ref(null)
const pickQty = ref(1)
const allowPartial = ref(false)
const previewData = ref(null)
const result = ref(null)
const confirmError = ref('')
const previewError = ref('')
const loading = ref(false)
const checking = ref(false)
const checkRows = ref([])
const lotMeta = ref({})  // lot metadata snapshotted at confirm time (lots may leave the shelf after)

const layerMap = { upper: '上层', mid: '中层', lower: '下层' }
const layerLabel = l => layerMap[l] || l

const eligible = computed(() => rows.value
  .filter(r => r.qty_remain > 0 && r.data_quality !== 'dirty')
  .sort((a, b) => a.item_id - b.item_id || (a.expiry || '9999').localeCompare(b.expiry || '9999')))
const excludedDirty = computed(() => rows.value.filter(r => r.qty_remain > 0 && r.data_quality === 'dirty'))

const itemOptions = computed(() => {
  const m = new Map()
  for (const r of eligible.value) {
    if (!m.has(r.item_id)) m.set(r.item_id, { id: r.item_id, name: r.name, unit: r.unit, layer: r.layer, avail: 0 })
    m.get(r.item_id).avail += r.qty_remain
  }
  return [...m.values()].map(x => ({ ...x, layerLabel: layerLabel(x.layer) }))
})
const pickAvailable = computed(() => itemOptions.value.some(i => i.id === pickId.value))
function nameOf(id) { return items.value.find(i => i.id === id)?.name || id }
function unitOf(id) { return items.value.find(i => i.id === id)?.unit || '' }
function availOf(id) {
  return eligible.value.filter(r => r.item_id === id).reduce((s, r) => s + r.qty_remain, 0)
}
function lotRemain(lotId) { return rows.value.find(r => r.id === lotId)?.qty_remain ?? 0 }
function fmt(v) { return (v === null || v === undefined) ? '-' : String(Math.round(v * 1000) / 1000) }

const groupedPreview = computed(() => {
  if (!previewData.value) return []
  return previewData.value.items.map(it => ({
    ...it,
    deductions: it.deductions.map(d => ({ ...d, _remainBefore: lotRemain(d.lot_id) })),
  }))
})

async function load() {
  const [f, its] = await Promise.all([api('/fridge'), api('/items')])
  rows.value = f; items.value = its
}
onMounted(async () => {
  await load()
  if (itemOptions.value[0]) pickId.value = itemOptions.value[0].id
})

function addLine() {
  const opt = itemOptions.value.find(i => i.id === pickId.value)
  if (!opt) return
  if (!lines.value.find(l => l.item_id === opt.id)) {
    lines.value.push({ item_id: opt.id, qty: Math.min(pickQty.value || 1, opt.avail) })
  }
  previewData.value = null; result.value = null; confirmError.value = ''
}
function removeLine(id) {
  lines.value = lines.value.filter(l => l.item_id !== id)
  previewData.value = null; result.value = null
}

function payload() {
  return {
    items: lines.value.map(l => ({ item_id: l.item_id, qty: l.qty })),
    allow_partial: allowPartial.value,
  }
}

async function preview() {
  previewError.value = ''; previewData.value = null; result.value = null
  try {
    await load()  // plan against freshest on-shelf state
    previewData.value = await api('/combo/preview', { method: 'POST', body: JSON.stringify(payload()) })
  } catch (e) { previewError.value = e.message }
}

function parseError(e) {
  try { return JSON.stringify(JSON.parse(e.message), null, 2) } catch { return e.message }
}

async function confirm() {
  loading.value = true; confirmError.value = ''; result.value = null; checkRows.value = []
  try {
    const res = await api('/combo/confirm', { method: 'POST', body: JSON.stringify(payload()) })
    // freeze lot identity/name/layer BEFORE refreshing, so consumed-off lots still render
    lotMeta.value = {}
    for (const d of res.deductions) {
      const r = rows.value.find(x => x.id === d.lot_id)
      if (r) lotMeta.value[d.lot_id] = { name: r.name, layer: r.layer, item_id: r.item_id, data_quality: r.data_quality }
    }
    result.value = res
    await load()
  } catch (e) {
    confirmError.value = parseError(e)
    await load()
    previewData.value = null
  } finally { loading.value = false }
}

const allMatch = computed(() => checkRows.value.length > 0 && checkRows.value.every(r => r.match))
const someChangedAfter = computed(() => checkRows.value.some(r => r.changedAfter))

async function crossCheck() {
  checking.value = true
  try {
    const fridge = await api('/fridge')  // 全层实时
    const out = []
    for (const d of result.value.deductions) {
      const meta = lotMeta.value[d.lot_id] || {}
      const live = fridge.find(r => r.id === d.lot_id)  // consumed lots leave /fridge -> undefined
      const resp = result.value.lots_remaining[d.lot_id] || {}
      const actual = live ? live.qty_remain : null
      const respRemain = resp.qty_remain ?? 0
      const sameAsResponse = live ? Math.abs(actual - respRemain) < 1e-9
                                  : resp.status === 'consumed' && respRemain === 0
      out.push({
        lot_id: d.lot_id,
        name: meta.name ?? '',
        layer: meta.layer ?? '',
        take: d.take,
        respRemain,
        respStatus: resp.status,
        actual,
        dirty: meta.data_quality === 'dirty',
        match: sameAsResponse,
        changedAfter: !sameAsResponse && actual !== null && actual < respRemain,
      })
    }
    checkRows.value = out
  } finally { checking.value = false }
}
</script>
