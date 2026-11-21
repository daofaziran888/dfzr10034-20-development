import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type VoucherSummary = {
  voucher_no: string
  partition: string
  copy_id: string
  generation: number
  row_count: number
  content_digest: string
  status: string
  receipt_no: string
  signed_at: string
}

export type VoucherDetail = VoucherSummary & {
  partition: string
  schema: string[]
  rows: EntryRow[]
  row_digests: string[]
  digest_algo: string
}

export type PartitionRecord = {
  ok: boolean
  partition: string
  restored?: boolean
  message?: string
  row_check?: { index: number; matched: boolean; digest: string }[]
  voucher_no: string
  generation: number
  row_count: number
  schema: string[]
  rows: EntryRow[]
  row_digests: string[]
  content_digest: string
  source: string
  synced_at: string
  error?: string
}

export type ClearanceResult = {
  ok: boolean
  eligible: boolean
  reason: string
  row_count?: number
  voucher_row_count?: number
  content_matched?: boolean
}

export async function listVouchers(partition?: string): Promise<VoucherSummary[]> {
  const query = partition ? `?partition=${encodeURIComponent(partition)}` : ''
  const response = await request(`/api/cold-integrity/vouchers${query}`)
  if (!response.ok) throw new Error('凭单列表读取失败')
  const payload = (await response.json()) as { items: VoucherSummary[] }
  return payload.items
}

export async function getVoucher(voucherNo: string): Promise<VoucherDetail> {
  const response = await request(`/api/cold-integrity/vouchers/${encodeURIComponent(voucherNo)}`)
  if (!response.ok) throw new Error('凭单读取失败')
  return (await response.json()) as VoucherDetail
}

export async function readReadablePartition(partition: string): Promise<PartitionRecord> {
  const response = await request(`/api/cold-integrity/partitions/${encodeURIComponent(partition)}/readable`)
  if (!response.ok) throw new Error('可读分区读取失败')
  return (await response.json()) as PartitionRecord
}

export async function checkClearance(
  partition: string,
  voucherNo: string,
): Promise<ClearanceResult> {
  const query = new URLSearchParams({ voucher_no: voucherNo }).toString()
  const response = await request(`/api/cold-integrity/partitions/${encodeURIComponent(partition)}/clearance?${query}`)
  if (!response.ok) throw new Error('清退资格校验失败')
  return (await response.json()) as ClearanceResult
}

export async function restoreVoucher(voucherNo: string): Promise<PartitionRecord> {
  const response = await request(`/api/cold-integrity/vouchers/${encodeURIComponent(voucherNo)}/restore`, {
    method: 'POST',
  })
  return (await response.json()) as PartitionRecord
}
