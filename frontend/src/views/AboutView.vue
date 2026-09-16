<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, message } from '../api'
import { bootstrap, me } from '../state'
import { isDesktop } from '../platform'
import { appVersion, buildTime, releasesUrl } from '../version'
import { checkUpdate, release, checkingUpdate, installingUpdate, installDesktopUpdate, updateError, downloadProgress, openReleases, type UpdateStatus } from '../updates'
import { renderMarkdown } from '../markdown'
import SettingsLayout from '../components/SettingsLayout.vue'
import SettingsSection from '../components/SettingsSection.vue'
import UiIcon from '../components/UiIcon.vue'
import brandIcon from '../icon.svg'

const server = ref<UpdateStatus>(), serverError = ref(''), requesting = ref(false), reconnecting = ref(false)
const canManageServer = computed(() => !isDesktop && me.value?.instance_admin)
const serverVersion = computed(() => bootstrap.value?.app_version)
const pageOutdated = computed(() => !isDesktop && serverVersion.value && serverVersion.value !== appVersion)
const activeStages = ['queued', 'downloading', 'verifying', 'backing_up', 'upgrading', 'restarting', 'rolling_back']
const serverBusy = computed(() => requesting.value || !!server.value && activeStages.includes(server.value.stage))
const busy = computed(() => checkingUpdate.value || installingUpdate.value || serverBusy.value)
const stageLabels: Record<string, string> = { queued: '等待开始升级', downloading: '正在下载更新', verifying: '正在校验更新包', backing_up: '正在备份数据', upgrading: '正在升级服务器', restarting: '正在启动新版本', rolling_back: '正在恢复旧版本', complete: '服务器已升级完成', failed: '升级未完成' }
const percentage = computed(() => downloadProgress.value?.total ? Math.min(100, Math.round(downloadProgress.value.downloaded * 100 / downloadProgress.value.total)) : null)
const progressText = computed(() => downloadProgress.value?.stage === 'installing' ? '正在安装，即将重启…' : `正在下载${percentage.value === null ? '…' : ` ${percentage.value}%`}`)
const releaseNotes = computed(() => renderMarkdown(release.value?.notes || ''))
let timer: ReturnType<typeof setTimeout> | undefined, disposed = false

async function pollStatus() {
  if (!canManageServer.value || disposed) return
  try {
    server.value = await api.request<UpdateStatus>('/instance/update')
    reconnecting.value = false
    if (server.value.stage === 'complete') {
      bootstrap.value = await api.request('/bootstrap')
    }
  } catch { if (serverBusy.value) reconnecting.value = true }
  finally { if (!disposed) timer = setTimeout(pollStatus, serverBusy.value ? 2000 : 10000) }
}
async function upgradeServer() {
  if (busy.value || !canManageServer.value || !server.value?.enabled || !release.value?.installable || !release.value.latest_version) return
  requesting.value = true
  serverError.value = ''
  try {
    server.value = await api.request<UpdateStatus>('/instance/update', 'POST', { version: release.value.latest_version })
    clearTimeout(timer)
    void pollStatus()
  } catch (error) { serverError.value = message(error) }
  finally { requesting.value = false }
}
function refreshPage() { window.location.reload() }
onMounted(() => { void pollStatus() })
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <SettingsLayout title="关于与更新">
    <template v-if="!me" #actions><RouterLink :to="isDesktop ? '/connections' : '/login'">返回{{ isDesktop ? '客户端' : '登录' }}</RouterLink></template>
    <SettingsSection title="Cursor Panel" description="版本信息与软件更新。">
      <div class="about-brand"><img :src="brandIcon" width="56" height="56" alt="Cursor Panel" /><div><h2>Cursor Panel</h2><p>{{ isDesktop ? '客户端' : 'Web 页面' }} <strong data-testid="app-version">v{{ appVersion }}</strong><span v-if="!isDesktop && serverVersion" class="about-server-version">服务器 v{{ serverVersion }}</span></p></div></div>
      <p class="field-hint about-build">构建于 {{ new Date(buildTime).toLocaleString() }}</p>
      <p v-if="pageOutdated" class="form-success" role="status"><UiIcon name="info" :size="15" />服务器已有新页面，刷新后即可使用。</p>
      <button v-if="pageOutdated" class="primary" @click="refreshPage">刷新到新版本</button>
    </SettingsSection>
    <SettingsSection title="软件更新" :description="isDesktop ? '下载完成后自动安装并重启，保留本地账号和设置。' : '检查正式发布的最新版本。服务器升级由实例管理员执行。'">
      <div class="about-update-actions"><button class="subtle-button" :disabled="busy || (!isDesktop && !me)" @click="checkUpdate"><UiIcon name="refresh" :class="{ spinning: checkingUpdate }" :size="15" />{{ checkingUpdate ? '正在检查…' : '检查更新' }}</button><a :href="releasesUrl" target="_blank" rel="noopener noreferrer" class="about-release-link" @click="openReleases">发行记录<UiIcon name="external" :size="13" /></a></div>
      <p v-if="!isDesktop && !me" class="field-hint"><RouterLink to="/login">登录</RouterLink>后可以检查更新。</p>
      <p v-if="updateError" class="error" role="alert">{{ updateError }}</p>
      <div v-if="release" class="about-release" aria-live="polite">
        <p class="about-release-title"><UiIcon :name="release.available ? 'info' : 'check'" :size="17" />{{ release.available ? `发现新版本 v${release.latest_version}` : release.latest_version ? '当前已是最新版本' : '暂时没有正式发布的版本' }}</p>
        <template v-if="release.available">
          <p v-if="!release.installable" class="field-hint">这个版本尚未提供可自动安装的更新包，请查看发行记录。</p>
          <button v-if="isDesktop && release.installable" class="primary" :disabled="busy" @click="installDesktopUpdate"><UiIcon name="download" :size="15" />{{ installingUpdate ? '正在升级…' : '一键升级并重启' }}</button>
          <template v-if="!isDesktop && release.installable">
            <button v-if="canManageServer" class="primary" :disabled="busy || !server?.enabled" @click="upgradeServer"><UiIcon name="download" :size="15" />{{ serverBusy ? '正在升级…' : '一键升级服务器' }}</button>
            <p v-else class="field-hint">请联系实例管理员升级服务器。</p>
          </template>
          <details v-if="release.notes" class="about-notes"><summary>本次更新内容</summary><div class="about-notes-content" v-html="releaseNotes" /></details>
        </template>
      </div>
      <div v-if="installingUpdate" class="about-progress" role="status"><p>{{ progressText }}</p><progress :value="percentage ?? undefined" max="100" aria-label="更新下载进度" /></div>
    </SettingsSection>
    <SettingsSection v-if="canManageServer" title="服务器升级" description="升级前自动保留原数据，启动检查失败时恢复旧版本。">
      <p v-if="server && !server.enabled" class="field-hint">该实例尚未连接自动升级服务。<RouterLink to="/docs/updates">查看部署说明</RouterLink></p>
      <p v-else-if="server?.stage === 'idle'" class="field-hint">自动升级服务已就绪。检查到新版本后即可一键升级。</p>
      <p v-if="server && server.stage !== 'idle'" role="status" :class="server.stage === 'failed' ? 'error' : 'form-success'"><UiIcon v-if="serverBusy" name="refresh" class="spinning" :size="15" />{{ reconnecting ? '服务器正在重启，等待重新连接…' : stageLabels[server.stage] }}<span v-if="server.version"> · v{{ server.version }}</span></p>
      <p v-if="server?.message" class="field-hint">{{ server.message }}</p>
      <p v-if="serverError" class="error" role="alert">{{ serverError }}</p>
    </SettingsSection>
  </SettingsLayout>
</template>
<style>
.about-brand { display: flex; align-items: center; gap: 16px; }
.about-brand h2 { margin: 0 0 5px; font-size: 18px; font-weight: 600; }
.about-brand p { margin: 0; color: var(--dim); font-size: 12px; }
.about-brand strong { color: var(--fg); font-weight: 500; margin-left: 5px; }
.about-server-version { display: inline-block; margin-left: 16px; }
.about-build { margin: 15px 0 0; }
.about-update-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 20px; }
.about-release-link { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--dim); }
.about-release { margin-top: 22px; }
.about-release-title { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--fg); }
.about-notes { margin-top: 20px; color: var(--dim); font-size: 12px; }
.about-notes summary { width: fit-content; cursor: pointer; color: var(--fg); font-weight: 500; }
.about-notes-content { max-height: 340px; overflow: auto; overscroll-behavior: contain; scrollbar-width: thin; margin-top: 14px; padding: 3px 8px 3px 18px; border-left: 2px solid var(--line); line-height: 1.75; overflow-wrap: anywhere; }
.about-notes-content > :first-child { margin-top: 0; }
.about-notes-content > :last-child { margin-bottom: 0; }
.about-notes-content :is(h1, h2, h3, h4, h5, h6) { color: var(--fg); font-weight: 600; line-height: 1.45; }
.about-notes-content h1 { margin: 0 0 14px; font-size: 15px; }
.about-notes-content h2 { margin: 20px 0 9px; font-size: 13.5px; }
.about-notes-content :is(h3, h4, h5, h6) { margin: 16px 0 7px; font-size: 12.5px; }
.about-notes-content p { margin: 0 0 11px; }
.about-notes-content :is(ul, ol) { margin: 0 0 12px; padding-left: 21px; }
.about-notes-content li { padding-left: 2px; }
.about-notes-content li + li { margin-top: 5px; }
.about-notes-content strong { color: var(--fg); font-weight: 600; }
.about-notes-content a { text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--accent) 45%, transparent); text-underline-offset: 3px; }
.about-notes-content code { padding: 1px 4px; border: 1px solid var(--line); border-radius: 4px; background: var(--code-bg); color: var(--fg); font-size: 11px; }
.about-notes-content pre { margin: 12px 0; max-height: none; white-space: pre; line-height: 1.65; }
.about-notes-content pre code { padding: 0; border: 0; background: transparent; }
.about-notes-content blockquote { margin: 12px 0; padding: 1px 0 1px 12px; border-left: 2px solid var(--accent); color: var(--dimmer); }
.about-notes-content blockquote p { margin: 0; }
.about-notes-content hr { margin: 17px 0; border: 0; border-top: 1px solid var(--line); }
.about-notes-content table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }
.about-notes-content :is(th, td) { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: left; }
.about-notes-content th { color: var(--fg); font-weight: 600; }
.about-progress { margin-top: 18px; font-size: 12px; color: var(--dim); }
.about-progress progress { display: block; width: 100%; max-width: 380px; height: 6px; accent-color: var(--accent); }
</style>
