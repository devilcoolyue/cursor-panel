<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { Account } from '../api'
import { money, timeText } from '../format'
import { cardDisplay } from '../card-display'
import { cycleRemaining, percent, quotaLimitHint, quotaTone } from '../quota'
import QuotaBar from './QuotaBar.vue'
import UiTooltip from './UiTooltip.vue'
import UiPopover from './UiPopover.vue'
import UiIcon from './UiIcon.vue'

const props = defineProps<{ account: Account; spaceName: string; now: number; refreshing?: boolean; refreshError?: string }>()
const emit = defineEmits<{ detail: [group: string] }>()
const card = ref<HTMLElement>()
let restoreRefreshFocus = false
watch(() => props.refreshing, async refreshing => {
  if (refreshing) { restoreRefreshFocus = !!card.value?.contains(document.activeElement); return }
  await nextTick()
  if (restoreRefreshFocus && !props.refreshing && card.value?.isConnected && !document.querySelector('dialog[open]')
    && (document.activeElement === document.body || card.value.contains(document.activeElement))) {
    card.value.querySelector<HTMLButtonElement>('button[aria-label="刷新"]')?.focus({ preventScroll: true })
  }
  restoreRefreshFocus = false
})
const metaRows = computed(() => [cardDisplay.stats, cardDisplay.reset, cardDisplay.spend, cardDisplay.onDemand, cardDisplay.grok && props.account.data?.grok_weekly].filter(Boolean).length)
const remaining = computed(() => props.account.data?.quota?.overall?.remaining_pct)
const exhausted = computed(() => remaining.value != null && remaining.value <= 0)
const cycle = computed(() => cycleRemaining(props.account.data?.cycle?.reset_at, props.account.data?.cycle?.start, props.now))
const status = computed(() => props.account.auth_invalid || props.account.expired ? '需要重新授权' : props.account.stale ? '刷新失败 · 保留上次数据' : props.account.pending ? '等待首次刷新' : props.account.error_kind ? '暂时无法查询' : '授权有效')
const ribbon = computed(() => remaining.value == null ? null : exhausted.value ? '燃尽' : remaining.value <= 10 ? '告急' : remaining.value <= 30 ? '偏紧' : '充足')
const elapsed = computed(() => {
  if (!props.account.ok_at) return '尚无数据'
  const minutes = Math.max(0, Math.floor((props.now - props.account.ok_at * 1000) / 60000))
  return minutes < 1 ? '刚刚' : minutes < 60 ? `${minutes} 分钟前` : minutes < 1440 ? `${Math.floor(minutes / 60)} 小时前` : `${Math.floor(minutes / 1440)} 天前`
})
const overLimit = computed(() => {
  const limit = props.account.data?.quota?.overall?.limit_usd
  const spent = props.account.data?.spend_usd?.total
  return limit != null && spent != null && spent > limit + .005
})
const spendHint = computed(() => {
  const plan = props.account.data?.plan, overall = props.account.data?.quota?.overall, spend = props.account.data?.spend_usd
  const included = plan?.included_usd == null ? '订阅包含额度暂无数据' : `订阅本身含额度 ${money(plan.included_usd)}`
  if (overall?.limit_usd == null) return `${included}。${quotaLimitHint(overall)}`
  const over = overLimit.value ? `已超出上限 ${money(spend!.total! - overall.limit_usd)}。` : ''
  return `${over}${included}。分母 ${money(overall.limit_usd)} 是本周期两类模型额度的合计上限。${quotaLimitHint(overall)}`
})
const statTime = (value: number) => new Date(value * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
const resetDate = (value?: string | null) => value ? new Date(value).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replaceAll('/', '-') : '—'
</script>
<template>
  <article ref="card" class="account-card" :class="{ exhausted: exhausted && !refreshing, dead: !refreshing && (account.auth_invalid || account.expired), refreshing }" :aria-busy="refreshing || undefined" data-account>
    <span v-if="!refreshing && cardDisplay.ribbon && ribbon" class="ribbon" :class="exhausted ? 'dry' : quotaTone(remaining)" aria-hidden="true"><b>{{ ribbon }}</b></span>
    <span v-if="!refreshing && cardDisplay.watermark && exhausted" class="burnout-watermark" aria-hidden="true"><span>燃</span><span>尽</span><span>了</span></span>
    <span v-if="!refreshing && cardDisplay.ring && cycle" class="cycle-ring" :aria-label="`额度刷新：${cycle.number}${cycle.unit}`" :title="`额度刷新 ${timeText(account.data?.cycle?.reset_at)}`">
      <svg viewBox="0 0 44 44" aria-hidden="true"><circle class="ring-track" cx="22" cy="22" r="19" /><circle class="ring-fill" cx="22" cy="22" r="19" :stroke-dasharray="2 * Math.PI * 19" :stroke-dashoffset="2 * Math.PI * 19 * (1 - cycle.passed)" /></svg><span><b>{{ cycle.number }}</b>{{ cycle.unit }}</span>
    </span>
    <div v-show="!refreshing" class="card-quick-actions"><slot name="quick-actions" /></div>
    <header class="account-card-heading"><div class="account-name-line"><span v-if="!refreshing" class="card-auth-status" role="img" :aria-label="status" :title="status"><i class="status-dot" :class="{ invalid: account.auth_invalid || account.expired, stale: account.stale || account.pending || !!account.error_kind }" /></span><h2 :title="account.label">{{ account.label }}</h2><span v-if="!refreshing && cardDisplay.plan" class="plan">{{ account.data?.plan?.name || '套餐未知' }}</span></div><div v-if="cardDisplay.department || cardDisplay.email" class="account-identity-line"><span v-if="cardDisplay.department" class="department-mark" :title="account.tags.join(' · ') || spaceName">{{ account.tags.join(' · ') || spaceName }}</span><span v-if="cardDisplay.email" class="account-email" :title="account.email || ''">{{ account.email || '邮箱未知' }}</span></div><span v-if="refreshing" class="card-refresh-status" role="status" :aria-label="`${account.label}：正在向 Cursor 重新取数…`" title="正在向 Cursor 重新取数…"><UiIcon name="refresh" :size="13" class="spinning" />刷新中…</span></header>
    <div class="card-data">
    <div class="card-data-content" :class="{ 'card-data-hidden': refreshing }" :inert="refreshing || undefined" :aria-hidden="refreshing || undefined">
    <div class="card-quotas"><QuotaBar :show-limit="cardDisplay.limit" name="Cursor Models" :slot="account.data?.quota?.cursor_models" :interactive="account.capabilities.detail" @detail="emit('detail', 'cursor_models')" /><QuotaBar :show-limit="cardDisplay.limit" name="Other Models" :slot="account.data?.quota?.other_models" :interactive="account.capabilities.detail" @detail="emit('detail', 'other_models')" /><QuotaBar :show-limit="cardDisplay.limit" name="综合" :slot="account.data?.quota?.overall" :interactive="account.capabilities.detail" @detail="emit('detail', 'overall')" /></div>
    <dl v-if="cardDisplay.stats || cardDisplay.reset || cardDisplay.spend || cardDisplay.onDemand || (cardDisplay.grok && account.data?.grok_weekly)" class="card-meta">
      <div v-if="cardDisplay.stats"><dt>最后统计</dt><dd :title="timeText(account.ok_at)">{{ account.ok_at ? `${statTime(account.ok_at)} · ${elapsed}` : '尚无数据' }}</dd></div>
      <div v-if="cardDisplay.reset"><dt>额度刷新</dt><dd>{{ timeText(account.data?.cycle?.reset_at) }}<template v-if="cycle"> · {{ cycle.unit ? `剩${cycle.number}${cycle.unit === '时' ? '小时' : '天'}` : '已到刷新时间' }}</template></dd></div>
      <div v-if="cardDisplay.spend"><dt><UiTooltip :text="spendHint" v-slot="{ id, toggle }"><button type="button" class="meta-hint" :aria-describedby="id" @click="toggle">本周期消费</button></UiTooltip></dt><dd><span :class="{ 'danger-text': overLimit }">{{ account.data?.spend_usd?.total == null ? '—' : money(account.data.spend_usd.total) }}</span><span v-if="account.data?.spend_usd?.total != null" class="spend-limit" :title="quotaLimitHint(account.data?.quota?.overall)"> / {{ account.data?.quota?.overall?.limit_usd == null ? '—' : `${account.data.quota.overall.limit_source === 'exhaustion' ? '≈' : ''}${money(account.data.quota.overall.limit_usd)}` }}</span></dd></div>
      <div v-if="cardDisplay.onDemand"><dt>按量付费</dt><dd>{{ account.data?.on_demand == null ? '—' : account.data.on_demand.enabled ? '开启' : '关闭' }}</dd></div>
      <div v-if="cardDisplay.grok && account.data?.grok_weekly"><dt><UiPopover label="Grok Bot 周额度说明" placement="top" :width="250" hover class="grok-hint">
        <template #trigger="{ id, open, toggle }"><button type="button" class="meta-hint" aria-haspopup="dialog" :aria-expanded="open" :aria-controls="id" @click="toggle">Grok Bot 周额度</button></template>
        <template #default><div class="grok-hint-content">Grok Bot 是 x.ai 的独立 App，使用 Cursor 账号登录，额度单独结算，不占用上方的 Cursor Models / Other Models，按周重置。拥有额度但尚未使用时显示剩余 100%。<br />下载地址：<a href="https://cursor.com/download/bot" target="_blank" rel="noopener noreferrer">https://cursor.com/download/bot<UiIcon name="external" :size="11" /></a></div></template>
      </UiPopover></dt><dd><span :class="quotaTone(account.data.grok_weekly.remaining_pct)">{{ account.data.grok_weekly.remaining_pct == null ? '—' : `剩 ${percent(account.data.grok_weekly.remaining_pct)}` }}</span><span class="reset-date"> · {{ resetDate(account.data.grok_weekly.reset_at) }} 重置</span></dd></div>
    </dl>
    <p v-if="account.stale || account.auth_invalid || account.expired || account.pending || account.error_kind" class="card-notice" :class="{ 'danger-text': account.auth_invalid || account.expired }" :title="account.error_message || undefined">{{ status }}</p>
    </div>
    <div v-if="refreshing" class="card-refresh-skeleton" aria-hidden="true">
      <div class="card-skeleton-quotas"><div v-for="row in 3" :key="row" class="card-skeleton-quota"><div><span class="card-skeleton-line skeleton-label" /><span class="card-skeleton-line skeleton-value" /></div><span class="card-skeleton-line skeleton-track" /></div></div>
      <div v-if="metaRows" class="card-skeleton-meta"><div v-for="row in metaRows" :key="row"><span class="card-skeleton-line" /><span class="card-skeleton-line" /></div></div>
    </div>
    </div>
    <p v-if="refreshError && !refreshing" class="card-notice danger-text" role="alert">{{ account.data ? '刷新失败，已保留上次数据。' : '刷新失败。' }}{{ refreshError }}</p>
  </article>
</template>
