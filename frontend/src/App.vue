<template>
  <div>
    <div class="alert-bar" v-if="alerts.length">临期预警：{{ alerts.map(a => a.name + '(' + a.level + ')').join(' · ') }}</div>
    <div class="alert-bar" v-else>临期预警带：暂无紧急批次</div>
    <div class="wrap">
      <nav class="layer-tabs">
        <router-link to="/">全层</router-link>
        <router-link to="/layer/upper">上层</router-link>
        <router-link to="/layer/mid">中层</router-link>
        <router-link to="/layer/lower">下层</router-link>
        <router-link to="/inbound">入库</router-link>
        <router-link to="/consume">消费</router-link>
        <router-link to="/combo">先吃组合</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <router-view />
    </div>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from './api'
const alerts = ref([])
const route = useRoute()
// 顶条现算：每次进路由都重新拉取，跟随最新 warn_days
async function loadAlerts() { try { alerts.value = await api('/alerts') } catch { alerts.value = [] } }
watch(() => route.fullPath, loadAlerts)
onMounted(loadAlerts)
</script>
