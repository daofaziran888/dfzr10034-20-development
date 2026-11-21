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
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="voucherPanel" class="voucher-panel" data-testid="voucher-panel">
      <header class="voucher-head">
        <h3>冷副本完整性凭单</h3>
        <button class="btn ghost" type="button" data-testid="voucher-refresh" @click="loadVouchers">
          刷新凭单
        </button>
      </header>
      <p class="page-desc">
        凭单按交接代次保存逐行摘要，恢复与清退都以实际行内容为准，不以条数相等放行。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>凭单编号</th><th>冷副本</th><th>分区</th><th>交接代次</th>
            <th>行数</th><th>内容摘要</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="voucher in vouchers" :key="voucher.voucher_no" :data-testid="`voucher-${voucher.voucher_no}`">
            <td>{{ voucher.voucher_no }}</td>
            <td>{{ voucher.copy_id }}</td>
            <td>{{ voucher.partition }}</td>
            <td>{{ voucher.generation }}</td>
            <td>{{ voucher.row_count }}</td>
            <td class="mono">{{ voucher.content_digest.slice(0, 12) }}</td>
            <td>{{ voucher.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`restore-${voucher.voucher_no}`" @click="restoreByVoucher(voucher.voucher_no)">
                恢复
              </button>
              <button class="link" type="button" :data-testid="`clearance-${voucher.voucher_no}`" @click="checkByVoucher(voucher)">
                清退资格
              </button>
            </td>
          </tr>
          <tr v-if="!vouchers.length">
            <td colspan="8" class="empty-state">该资产暂无已签收凭单</td>
          </tr>
        </tbody>
      </table>
      <p v-if="voucherMessage" class="status-legend" data-testid="voucher-message">{{ voucherMessage }}</p>
      <table v-if="restoredRows.length" class="data-table" data-testid="restored-rows">
        <thead>
          <tr><th v-for="field in restoredFields" :key="field">{{ field }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in restoredRows" :key="index">
            <td v-for="field in restoredFields" :key="field">{{ row[field] ?? '—' }}</td>
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
import { computed, onMounted, ref } from 'vue'

import {
  checkClearance,
  createEntry,
  downloadEntries,
  listEntries,
  listVouchers,
  moduleMeta,
  restoreVoucher,
  runAction as applyAction,
} from '@/api/backend-service'
import type { VoucherSummary } from '@/api/backend-service'
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
const vouchers = ref<VoucherSummary[]>([])
const voucherMessage = ref('')
const restoredRows = ref<EntryRow[]>([])
const restoredFields = ref<string[]>([])
const voucherPartition = computed(() => {
  const first = rows.value[0]
  return String(first?.['源资产'] ?? first?.['目标资产'] ?? '')
})
const voucherPanel = computed(
  () => meta.key === 'cold_archive' || meta.key === 'lifecycle_policy' || meta.key === 'retention_job',
)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function loadVouchers() {
  if (!voucherPanel.value || !voucherPartition.value) {
    vouchers.value = []
    return
  }
  try {
    vouchers.value = await listVouchers(voucherPartition.value)
  } catch {
    vouchers.value = []
  }
}

async function restoreByVoucher(voucherNo: string) {
  voucherMessage.value = ''
  restoredRows.value = []
  restoredFields.value = []
  try {
    const payload = await restoreVoucher(voucherNo)
    if (!payload.restored) {
      voucherMessage.value = payload.message || '凭单校验失败，拒绝恢复'
      return
    }
    restoredRows.value = payload.rows
    restoredFields.value = payload.schema
    voucherMessage.value = `${payload.message}（第 ${payload.generation} 代，${payload.row_count} 行）`
  } catch (error) {
    voucherMessage.value = error instanceof Error ? error.message : '恢复失败'
  }
}

async function checkByVoucher(voucher: VoucherSummary) {
  voucherMessage.value = ''
  try {
    const result = await checkClearance(voucher.partition, voucher.voucher_no)
    voucherMessage.value = result.eligible
      ? `凭单 ${voucher.voucher_no}：可清退（${result.reason}）`
      : `凭单 ${voucher.voucher_no}：不可清退（${result.reason}）`
  } catch (error) {
    voucherMessage.value = error instanceof Error ? error.message : '清退资格校验失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    await loadVouchers()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
