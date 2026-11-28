<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button v-for="action in meta.actions" :key="action" class="link" type="button"
              :data-testid="`action-${row.id}-${action}`" @click="runAction(action, row)">{{ action }}</button>
            <button v-if="meta.key === 'sample_registry'" class="link" type="button"
              :data-testid="`detail-${row.id}`" @click="openDetail(row)">登记详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">暂无{{ meta.name }}数据，可先登记{{ meta.entity }}</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel" data-testid="registry-detail">
      <header class="detail-head">
        <h3>登记详情：{{ detail.样品编号 }} / {{ detail.采集站位 }}</h3>
        <button class="btn ghost" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <div><dt>当前状态</dt><dd data-testid="detail-status">{{ detail.status }}</dd></div>
        <div><dt>保存批次</dt><dd data-testid="detail-batch">{{ detail.保存批次 }}（{{ detail.批次状态 }}）</dd></div>
        <div><dt>实际来源</dt><dd data-testid="detail-source">{{ detail.实际来源 }}</dd></div>
        <div><dt>实际保管</dt><dd data-testid="detail-custodian">{{ detail.实际保管 }}</dd></div>
        <div><dt>取样资格</dt><dd data-testid="detail-sampling">{{ detail.取样资格 }}</dd></div>
        <div><dt>分析可用资格</dt><dd data-testid="detail-analysis">{{ detail.analysis_eligible ? '可用' : '不可用' }}</dd></div>
        <div><dt>关联实物</dt><dd data-testid="detail-physical">{{ detail.关联实物?.实物编号 }}（登记项 {{ detail.关联实物?.登记项编号 }}）</dd></div>
        <div><dt>后续处理</dt><dd data-testid="detail-next">{{ detail.后续处理 }}</dd></div>
      </dl>
      <h4>入库同步回执</h4>
      <table class="data-table" data-testid="detail-receipts">
        <thead>
          <tr><th>回执编号</th><th>来源模块</th><th>来源单号</th><th>来源批次</th><th>关联实物</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="receipt in detail.入库同步回执" :key="receipt.回执编号" :data-testid="`receipt-${receipt.回执编号}`">
            <td>{{ receipt.回执编号 }}</td>
            <td>{{ receipt.来源模块 }}</td>
            <td>{{ receipt.来源单号 }}</td>
            <td>{{ receipt.来源批次 }}</td>
            <td>{{ receipt.关联实物?.实物编号 }} / {{ receipt.关联实物?.采集站位 }}</td>
            <td>{{ receipt.状态 }}</td>
          </tr>
          <tr v-if="!detail.入库同步回执.length">
            <td colspan="6" class="empty-state">暂无入库同步回执</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { createEntry, downloadEntries, fetchEntry, listEntries, moduleMeta, runAction as applyAction } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const detail = ref<Record<string, any> | null>(null)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((r) => r.status === meta.statuses[1]).length },
  { label: meta.metrics[2], value: rows.value.filter((r) => r.status === meta.statuses[meta.statuses.length - 1]).length },
])

const statusSummary = computed(() =>
  meta.statuses.map((s) => ({ status: s, count: rows.value.filter((r) => String(r.status) === s).length })),
)

function resetFilters() { filters.value = {}; void reload() }

async function exportRows() {
  errorMessage.value = ''
  try { await downloadEntries(meta.key, filters.value) }
  catch (e) { errorMessage.value = e instanceof Error ? e.message : '导出失败' }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((f) => [f, '']))
  errorMessage.value = ''; showCreate.value = true
}

async function submitCreate() {
  creating.value = true; errorMessage.value = ''
  try {
    const r = await createEntry(meta.key, createValues.value)
    if (!r.ok) throw new Error(r.message)
    showCreate.value = false; await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '登记失败' }
  finally { creating.value = false }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const r = await applyAction(meta.key, Number(row.id), action)
    if (!r.ok) throw new Error(r.message)
    detail.value = null
    await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '操作失败' }
}

async function openDetail(row: EntryRow) {
  errorMessage.value = ''
  try {
    detail.value = await fetchEntry(meta.key, Number(row.id))
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '登记详情读取失败' }
}

watch(() => props.moduleKey, () => { detail.value = null; filters.value = {}; void reload() })

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items; total.value = payload.total
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '业务列表读取失败' }
}

onMounted(reload)
</script>

<style scoped>
.modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.35); display:flex; align-items:center; justify-content:center; z-index:100; }
.modal { background:#fff; border-radius:8px; padding:24px; width:480px; max-height:80vh; overflow-y:auto; }
.create-field { display:flex; flex-direction:column; gap:4px; margin-bottom:12px; }
.create-field span { font-size:13px; color:var(--text-secondary,#4a5568); }
.create-field input { padding:6px 8px; border:1px solid var(--border-color,#e2e8f0); border-radius:4px; font-size:13px; }
.detail-panel { margin-top:24px; padding:16px; border:1px solid var(--border-color,#e2e8f0); border-radius:8px; background:#fff; }
.detail-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
.detail-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px 16px; margin:0 0 16px; }
.detail-grid dt { font-size:12px; color:var(--text-secondary,#4a5568); }
.detail-grid dd { margin:2px 0 0; font-size:14px; }
</style>