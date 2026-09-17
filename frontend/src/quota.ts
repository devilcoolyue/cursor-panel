import type { Schema } from './api'

export const percent = (value: number) => `${Number(value.toFixed(1))}%`
export const quotaTone = (value?: number | null) => value == null ? 'unknown' : value <= 10 ? 'bad' : value <= 30 ? 'warn' : 'ok'
export function quotaLimitHint(slot?: Schema['QuotaSlot'] | null) {
  if (slot?.limit_usd != null && slot.limit_source === 'exhaustion') return '额度已耗尽且未使用按量付费；本周期消费接近此档位（超额不超过 1%），参考可见同套餐账号估算。金额为近似上限，不是官方确认值。'
  if (slot?.limit_usd != null && slot.limit_source === 'reference') return '本账号本账期手动填写的参考上限，仅供参考；剩余百分比仍按 Cursor 返回值显示。账期或套餐变化后自动失效。'
  if (slot?.limit_usd != null && slot.limit_inferred) return slot.limit_source === 'history'
    ? '参考本账号同一账期此前推算的上限，仅供参考；本次剩余百分比仍按 Cursor 返回值显示。'
    : '参考当前空间内你可见、账期重叠且容量相符的同套餐账号上限估算，仅供参考。'
  if (slot?.limit_usd != null) return '美元上限由 Cursor 的消费金额与用量比例推算，仅供参考。'
  const used = slot?.used_pct ?? (slot?.remaining_pct == null ? null : 100 - slot.remaining_pct)
  if (used == null) return '尚无额度数据，暂时无法推算美元上限。'
  if (used >= 99.99) return '用量已触顶，无法直接反算上限；当前也没有本账号同账期历史或可见同套餐账号的有效观测可供补齐。'
  if (used <= 0) return '尚无已用额度，且暂无本账号同账期历史或可见同套餐账号的有效观测可供补齐。'
  return '当前用量比例不足以可靠推算美元上限。剩余百分比仍按 Cursor 返回值显示。'
}
export function cycleRemaining(reset?: string | null, start?: string | null, now = Date.now()) {
  const end = reset ? Date.parse(reset) : NaN
  if (!Number.isFinite(end)) return null
  const hours = Math.max(0, Math.floor((end - now) / 3600000))
  const beginning = start ? Date.parse(start) : NaN
  const passed = Number.isFinite(beginning) && end > beginning ? Math.max(0, Math.min(1, (now - beginning) / (end - beginning))) : 0
  return { number: end <= now ? '已到' : hours < 1 ? '<1' : hours < 48 ? String(hours) : String(Math.floor(hours / 24)), unit: end <= now ? '' : hours < 48 ? '时' : '天', passed }
}
