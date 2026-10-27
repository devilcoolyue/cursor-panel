<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useRouter } from 'vue-router'
import { isDesktop, connectionId } from '../platform'
import { activeSpace, me } from '../state'
import { guideProgress, guideSteps, selectGuideStep, pauseGuide, dismissGuide } from '../help/onboarding'
import UiDialog from './UiDialog.vue'
import UiIcon from './UiIcon.vue'
const router = useRouter(), heading = ref<HTMLElement>()
const step = computed(() => guideSteps[guideProgress.step]!)
const local = computed(() => isDesktop && !connectionId.value)
const action = computed(() => {
  if (!me.value) return { to: '/login', label: '登录后开始操作' }
  switch (guideProgress.step) {
    case 0: return { to: '/accounts', label: '查看我的工作空间' }
    case 1: return { to: '/settings', label: '打开个人设置' }
    case 2: return { to: '/accounts', label: '前往账号与额度' }
    case 3: return { to: local.value ? '/accounts' : '/docs/switching', label: local.value ? '前往账号列表切换' : '查看切换方式与限制' }
    case 4: return { to: isDesktop ? '/connections' : '/docs/server-deploy', label: isDesktop ? '打开实例连接' : '查看 Linux 部署步骤' }
    default: return { to: activeSpace.value?.capabilities.manage_members ? '/workspace' : '/docs/permissions', label: activeSpace.value?.capabilities.manage_members ? '打开空间设置' : '查看权限设置说明' }
  }
})
async function select(index: number) { selectGuideStep(index); await nextTick(); heading.value?.focus({ preventScroll: true }); heading.value?.scrollIntoView({ block: 'nearest' }) }
async function go(to: string) { await router.push(to); pauseGuide() }
</script>
<template>
  <UiDialog title="新手指引" class="guide-dialog" wide @close="dismissGuide">
    <template #subtitle>6 步熟悉 Cursor 额度面板 · 可随时跳过，稍后重新查看</template>
    <div class="guide-layout">
      <nav class="guide-steps" aria-label="新手指引步骤">
        <p class="help-eyebrow">开始使用</p>
        <button v-for="(item, index) in guideSteps" :key="item.doc" :aria-current="guideProgress.step === index ? 'step' : undefined" @click="select(index)">
          <span class="guide-number">{{ String(index + 1).padStart(2, '0') }}</span><span><strong>{{ item.title }}</strong><small>{{ item.caption }}</small></span>
        </button>
        <button class="guide-restart" @click="select(0)"><UiIcon name="refresh" :size="13" />从头阅读</button>
      </nav>
      <section :key="guideProgress.step" class="guide-detail" aria-labelledby="guide-step-title">
        <p class="help-eyebrow"><UiIcon :name="step.icon" :size="15" />第 {{ guideProgress.step + 1 }} / 6 步<span v-if="guideProgress.step >= 4">团队使用 · 可选</span></p>
        <h3 id="guide-step-title" ref="heading" tabindex="-1">{{ step.title }}</h3>
        <template v-if="guideProgress.step === 0">
          <p class="guide-lead">一个客户端，可以管理本机账号，也可以连接团队部署的服务。</p>
          <div class="guide-environments"><div><UiIcon name="monitor" :size="24" /><strong>客户端 · 本地</strong><p>账号存在这台电脑<br />无需部署服务即可使用</p></div><UiIcon name="switch" :size="18" /><div><UiIcon name="server" :size="24" /><strong>Linux · 远程实例</strong><p>账号存在服务器<br />登录后按空间权限访问</p></div></div>
          <ol class="guide-instructions"><li>先看侧栏顶部的连接名称，再确认当前个人或团队空间。</li><li>只管理自己的账号，可以先完成本地步骤；团队用户再连接实例。</li></ol>
          <p class="guide-note">本地与远程数据分别保存，切换实例不会自动同步或上传本地账号。</p>
        </template>
        <template v-else-if="guideProgress.step === 1">
          <p class="guide-lead">先调整习惯用的显示方式，再决定是否让客户端后台运行。</p>
          <ol class="guide-instructions"><li><strong>调整外观。</strong>侧栏「显示偏好」选择皮肤、浅色／深色／自动；「卡片显示项」控制额度卡片展示内容。</li><li><strong>设置后台。</strong>客户端运行时默认自动刷新本地额度；「个人设置 → 桌面运行」中的「关闭窗口后驻留托盘」只控制关窗后是否继续运行。</li><li><strong>准备备份。</strong>添加账号后，在本地「个人设置 → 加密归档」导出归档，设置至少 12 位口令并分开保存。</li></ol>
          <p class="guide-note">后台刷新处理本地账号。Linux 服务当前没有周期额度刷新；显示偏好不会改变请求或账号权限。</p>
        </template>
        <template v-else-if="guideProgress.step === 2">
          <p class="guide-lead">先添加一个账号，再认识卡片上的剩余额度和更新时间。</p>
          <ol class="guide-instructions"><li><strong>添加账号。</strong>在「账号与额度」点击「添加账号」，填写名称和标签，点「在浏览器中授权」，在打开的 Cursor 确认页用该账号确认登录。</li><li><strong>查看额度。</strong>卡片显示最后成功快照；额度条表示已用比例，右侧显示剩余比例。点「明细」查看模型用量。</li><li><strong>按需刷新。</strong>单个账号「刷新」查询最新额度；「重载列表」只重新读取快照。可按名称搜索、标签筛选和额度排序。</li></ol>
          <p class="guide-note">没有「添加账号」入口时，请联系空间所有者或管理员。成员加入团队后，需要单独获得账号授权。</p>
        </template>
        <template v-else-if="guideProgress.step === 3">
          <p class="guide-lead">本地客户端可把已授权账号切换到这台电脑上的 Cursor。</p>
          <ol class="guide-instructions"><li><strong>做好准备。</strong>先安装并打开一次 Cursor，保存编辑中的文件。当前原生切换支持 macOS 和 Windows 的常见安装位置。</li><li><strong>确认后执行。</strong>在账号卡片点「切换」，核对目标并勾选已保存工作，再点「开始切换」。也可选择「终端执行」。</li><li><strong>核对结果。</strong>客户端会正常退出 Cursor、备份、更新登录并重启；完成后在 Cursor 内确认当前账号。</li></ol>
          <p class="guide-note">连接远程实例时，当前不提供桌面直接切换。Linux 服务的部署不代表支持 Linux 本机切换；网页的手工命令流程见文档。</p>
        </template>
        <template v-else-if="guideProgress.step === 4">
          <p class="guide-lead">已有团队服务地址时，只需添加实例，再到系统浏览器确认登录。</p>
          <ol class="guide-instructions"><li><strong>添加实例。</strong>客户端侧栏「本地 · 切换实例 → 添加实例」，填写名称与 HTTPS 根地址，例如 https://panel.example.com；不要附加 /api 或子路径。</li><li><strong>浏览器登录。</strong>点「浏览器登录」，在打开的实例网页登录，核对用户、实例与设备后点「允许连接此设备」。</li><li><strong>回到客户端。</strong>授权完成后自动打开实例，选择对应空间查看账号。通过「返回本地账号」可回到本机数据。</li></ol>
          <p class="guide-note">没有服务器也可以继续本地使用。需要自行搭建时，文档「Linux 服务 → 首次部署」提供完整命令。重启客户端默认回到本地。</p>
        </template>
        <template v-else>
          <p class="guide-lead">团队权限分两层：空间角色决定管理范围，账号授权决定可查看和使用哪些账号。</p>
          <ol class="guide-instructions"><li><strong>邀请成员。</strong>在实例网页创建团队，再到「空间设置」填写受邀邮箱、选择角色并创建邀请。对方通过链接接受。</li><li><strong>逐账号授权。</strong>所有者或管理员在账号菜单打开「账号授权」，为成员选择「查看」或「使用」；只读成员只能获得「查看」。</li><li><strong>验证权限。</strong>让成员切到团队空间：查看权限可读额度与明细；使用权限还可刷新，并在支持的入口手工切换。</li></ol>
          <p class="guide-note">加入团队不等于获得全部账号。实例管理员负责登录用户管理，不自动获得其他空间的账号权限。</p>
        </template>
        <div class="guide-actions"><button class="primary" @click="go(action.to)">{{ action.label }}<UiIcon name="chevron" :size="14" /></button><button class="help-text-button" @click="go(`/docs/${step.doc}`)"><UiIcon name="book" :size="14" />阅读详细说明</button></div>
        <p class="guide-return-hint">前往功能页后，可通过「继续新手指引」回到这一步。</p>
      </section>
    </div>
    <template #footer>
      <button class="help-text-button guide-skip" @click="dismissGuide">暂时跳过</button>
      <span class="guide-progress-label">阅读进度 {{ guideProgress.step + 1 }} / 6</span>
      <button :disabled="guideProgress.step === 0" @click="select(guideProgress.step - 1)">上一步</button>
      <button v-if="guideProgress.step < 5" class="primary" @click="select(guideProgress.step + 1)">下一步<UiIcon name="chevron" :size="14" /></button>
      <button v-else class="primary" @click="dismissGuide"><UiIcon name="check" :size="14" />完成指引</button>
    </template>
  </UiDialog>
</template>
