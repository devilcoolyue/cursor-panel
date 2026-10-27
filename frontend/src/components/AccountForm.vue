<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import UiDialog from './UiDialog.vue'
import UiIcon from './UiIcon.vue'
import AccountTagPicker from './AccountTagPicker.vue'
import { api, accountPath, isAbort, spacePath, message, type Account, type AuthorizationProgress, type AuthorizationStarted } from '../api'
import { isDesktop, openCursorLogin } from '../platform'
const props = defineProps<{ workspaceId: string; account?: Account; authorize?: boolean; tagCatalog: Record<string, number> }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const label = ref(props.account?.label || ''), tags = ref([...(props.account?.tags || [])]), busy = ref(false), error = ref('')
const browser = computed(() => props.authorize || !props.account)
const pending = ref<{ id: string; url: string } | null>(null)
const controller = new AbortController()
let pollTimer: number | undefined
const referenceFields = [{ key: 'cursor_models', label: 'Cursor Models' }, { key: 'other_models', label: 'Other Models' }, { key: 'overall', label: '综合' }] as const
const references = ref(Object.fromEntries(referenceFields.map(({ key }) => {
  const slot = props.account?.data?.quota?.[key]
  return [key, slot?.limit_source === 'reference' ? String(slot.limit_usd) : '']
})))
const referencesChanged = ref(false)
const authorizationPath = (id?: string) => `${spacePath(props.workspaceId)}/authorizations${id ? '/' + encodeURIComponent(id) : ''}`
onBeforeUnmount(() => {
  controller.abort(); window.clearTimeout(pollTimer)
  // Closing the dialog abandons the attempt; the server forgets it even if this request is lost.
  if (pending.value) api.request(authorizationPath(pending.value.id), 'DELETE').catch(() => {})
})
async function openLogin(url: string, tab?: Window | null) {
  try {
    if (isDesktop) await openCursorLogin(url)
    else if (tab && !tab.closed) tab.location.href = url
    else if (!window.open(url, '_blank', 'noopener,noreferrer')) throw new Error('blocked')
  } catch { error.value = '无法自动打开浏览器，请点击「重新打开确认页」。' }
}
async function authorizeInBrowser() {
  // Web pages open the tab during the click so popup blockers allow it, then navigate it.
  const tab = isDesktop ? null : window.open('', '_blank')
  if (tab) tab.opener = null
  try {
    const started = await api.request<AuthorizationStarted>(authorizationPath(), 'POST', { label: label.value.trim() || null,
      tags: [...new Set(tags.value)], ...(props.authorize && props.account ? { account_id: props.account.id } : {}) }, controller.signal)
    pending.value = { id: started.id, url: started.login_url }
    await openLogin(started.login_url, tab)
    pollTimer = window.setTimeout(poll, 2000)
  } catch (reason) { tab?.close(); throw reason }
}
async function poll() {
  if (!pending.value) return
  try {
    const progress = await api.request<AuthorizationProgress>(authorizationPath(pending.value.id) + '/poll', 'POST', undefined, controller.signal)
    if (progress.status === 'completed') { pending.value = null; emit('saved'); return }
    pollTimer = window.setTimeout(poll, 2000)
  } catch (reason) {
    if (isAbort(reason)) return
    pending.value = null; busy.value = false; error.value = message(reason)
  }
}
async function save() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    if (browser.value) return await authorizeInBrowser()
    const limits = Object.fromEntries(referenceFields.map(({ key }) => [key, references.value[key] ? Number(references.value[key]) : null]))
    if (referencesChanged.value && Object.values(limits).every(value => value != null) && Math.abs(limits.cursor_models! + limits.other_models! - limits.overall!) > .01) {
      error.value = '综合参考上限应等于两个模型分池之和。'; return
    }
    const changes = { label: label.value.trim() || null, tags: [...new Set(tags.value)],
      ...(referencesChanged.value ? { quota_reference: { cycle_start: props.account?.data?.cycle?.start, ...limits } } : {}) }
    await api.request<Account>(accountPath(props.account!), 'PATCH', changes, controller.signal)
    emit('saved')
  } catch (reason) { error.value = message(reason) }
  finally { if (!pending.value) busy.value = false }
}
</script>
<template>
  <UiDialog class="account-form-dialog" :title="account ? (authorize ? '重新授权账号' : '编辑账号') : '添加 Cursor 账号'" dismiss-backdrop @close="emit('close')">
    <template #subtitle>{{ account?.email || '连接账号，查看用量与剩余额度。' }}</template>
    <form id="account-form" class="form-stack account-form" @submit.prevent="save">
      <label>账号名称<input v-model="label" :disabled="busy" maxlength="256" :required="!!account && !authorize" placeholder="例如：日常开发" autofocus /></label>
      <AccountTagPicker v-model="tags" :catalog="tagCatalog" :disabled="busy" />
      <details v-if="account && !authorize && account.data?.cycle?.start" class="quota-reference-fields">
        <summary>额度上限参考值（可选）</summary>
        <p class="field-hint">额度已用尽且历史缺失时，可填写旧版已知的上限（美元）。仅补齐当前账号本账期缺失的金额，不改变剩余百分比；账期或套餐变化后失效。清空可移除手动参考值。</p>
        <div class="reference-inputs"><label v-for="field in referenceFields" :key="field.key">{{ field.label }}<input v-model="references[field.key]" :aria-label="`${field.label} 参考上限`" type="number" min="0.01" max="1000000000" step="0.01" :disabled="busy" placeholder="自动" @input="referencesChanged = true" /></label></div>
      </details>
      <div v-if="pending" class="browser-authorization" role="status">
        <UiIcon name="refresh" class="spinning" :size="16" />
        <div>
          <strong>等待在浏览器中确认</strong>
          <p class="field-hint">已打开 Cursor 登录确认页。请确认浏览器当前登录的是要{{ authorize ? '重新授权' : '添加' }}的账号，再点击确认登录；完成后自动保存。</p>
          <button type="button" class="link-button" @click="openLogin(pending.url)"><UiIcon name="external" :size="13" />重新打开确认页</button>
        </div>
      </div>
      <p v-else-if="browser" class="field-hint browser-authorization-hint">点击「在浏览器中授权」后会打开 Cursor 登录确认页，与 Cursor 编辑器登录相同，无需复制 Cookie。请先确认浏览器当前登录的是要{{ authorize ? '重新授权' : '添加' }}的 Cursor 账号。</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </form>
    <template #footer><button type="button" @click="emit('close')">取消</button><button type="submit" form="account-form" class="primary" :disabled="busy"><UiIcon v-if="busy" name="refresh" class="spinning" :size="14" />{{ pending ? '等待确认…' : busy ? '正在处理…' : browser ? '在浏览器中授权' : '保存' }}</button></template>
  </UiDialog>
</template>
<style scoped>
.quota-reference-fields summary { cursor: pointer; font-size: 12px; color: var(--dim); }
.quota-reference-fields p { margin: 10px 0; }
.reference-inputs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.reference-inputs label { font-size: 11px; min-width: 0; }
.browser-authorization { display: flex; gap: 11px; padding: 13px 15px; border: 1px solid var(--line); border-radius: 10px; background: var(--input-bg); }
.browser-authorization > .ui-icon { margin-top: 2px; color: var(--accent); }
.browser-authorization strong { font-size: 12.5px; font-weight: 500; }
.browser-authorization .field-hint { margin: 4px 0 8px; }
.browser-authorization-hint { margin: 0; }
.link-button { display: inline-flex; align-items: center; gap: 5px; min-width: 0; padding: 0; border: 0; background: none; color: var(--accent); font-size: 11.5px; }
</style>
