/** Production page and native IPC adapter: check, progress, retry, and server authorization. */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile, mkdir } from 'node:fs/promises'
import { once } from 'node:events'
import { resolve, extname, join, relative, isAbsolute, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const root = fileURLToPath(new URL('../../', import.meta.url))
const dist = resolve(root, 'frontend/dist')
const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname
    const path = resolve(dist, '.' + (pathname === '/' ? '/index.html' : pathname))
    const relativePath = relative(dist, path)
    if (relativePath === '..' || relativePath.startsWith('..' + sep) || isAbsolute(relativePath)) throw Error('Invalid path')
    response.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[extname(path)] || 'application/octet-stream')
    response.end(await readFile(path))
  } catch { response.writeHead(404).end() }
})
server.listen(0, '127.0.0.1'); await once(server, 'listening')
const origin = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch()
const errors = []
const current = JSON.parse(await readFile(join(root, 'frontend/package.json'), 'utf8')).version
const nextVersion = current.split('.').map((part, index) => index === 2 ? Number(part) + 1 : part).join('.')
const fixtureMe = admin => ({ id: 'fixture-user', login: 'fixture@example.test', instance_admin: admin,
  workspaces: [{ id: 'fixture-space', name: '测试空间', kind: 'personal', role: 'owner', capabilities: {} }] })
const bootstrap = desktop => ({ mode: desktop ? 'local' : 'server', initialized: true, api_version: 1, app_version: current, capabilities: {} })
const latest = { current_version: current, latest_version: nextVersion, available: true, installable: true, notes: `# v${nextVersion} 更新内容

## 账号授权

- 修复 **Google OAuth** 账号无法导入的问题
- 错误 Cookie 会显示明确原因，不再误报网络失败

使用 \`WorkosCursorSessionToken\` 完成授权，详情见 [发行记录](https://example.test/releases/v${nextVersion})。

> 更新会保留本地账号和设置。

![不会自动加载的远程图片](https://example.test/tracker.png)

\`\`\`text
Cookie -> 桌面凭证
\`\`\`

<script>bad()</script>`, release_url: `https://github.com/devilcoolyue/cursor-panel/releases/tag/v${nextVersion}`, published_at: null }
await mkdir(join(root, 'output/playwright'), { recursive: true })
try {
  for (const admin of [false, true]) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 850 } })
    await context.addInitScript(admin => {
      localStorage.setItem('cursor.v2.onboarding', JSON.stringify({ version: 1, seen: true, active: false, step: 0 }))
      localStorage.setItem('cursor.v2.theme', admin ? 'light' : 'dark')
      localStorage.setItem('cursor.v2.skin', admin ? 'classic' : 'cyberpunk')
    }, admin)
    let installs = 0, checks = 0, job = { enabled: true, stage: 'idle', job_id: null, version: null, message: null }
    await context.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      let body
      if (path.endsWith('/bootstrap')) body = bootstrap(false)
      else if (path.endsWith('/auth/csrf')) body = { csrf_token: 'fixture-csrf' }
      else if (path.endsWith('/me')) body = fixtureMe(admin)
      else if (path.endsWith('/updates')) { checks++; body = latest }
      else if (path.endsWith('/instance/update')) {
        assert(admin, 'Only admins may request update control')
        if (route.request().method() === 'POST') {
          assert.equal(route.request().headers()['x-csrf-token'], 'fixture-csrf')
          assert.equal(route.request().postDataJSON().version, nextVersion)
          installs++; job = { enabled: true, stage: 'queued', job_id: 'fixture-job', version: nextVersion, message: null }
        }
        body = job
      } else throw Error('Unexpected route: ' + path)
      await route.fulfill({ json: body })
    })
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message))
    await page.goto(origin + '/#/about')
    await page.getByTestId('app-version').waitFor()
    await page.locator('.sidebar-brand .version-badge.has-update').waitFor()
    assert.equal(await page.locator('.version-badge-text').innerText(), `v${current}`)
    assert.equal(await page.locator('.sidebar-footer .sidebar-version').count(), 0)
    assert.equal(checks, 1, 'First authorized visit checks automatically')
    await page.reload()
    await page.locator('.sidebar-brand .version-badge.has-update').waitFor()
    assert.equal(checks, 1, 'A reload restores the result without repeating a recent check')
    const badge = page.locator('.version-badge')
    await badge.click()
    const popup = page.getByRole('dialog', { name: '版本与更新', exact: true })
    await popup.waitFor()
    await popup.getByText('每 4 小时自动检查', { exact: false }).waitFor()
    const badgeBounds = await badge.boundingBox(), logoBounds = await page.locator('.brand-link').boundingBox()
    assert(badgeBounds.x > logoBounds.x + logoBounds.width && Math.abs(badgeBounds.y + badgeBounds.height / 2 - logoBounds.y - logoBounds.height / 2) < 2)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    assert.equal(await page.locator('.version-update-dot').evaluate(el => getComputedStyle(el, '::after').animationName), 'none')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    assert.equal(await page.locator('.version-update-dot').evaluate(el => getComputedStyle(el, '::after').animationName), 'version-breathe')
    await page.screenshot({ path: join(root, `output/playwright/version-badge-${admin ? 'admin' : 'member'}.png`), fullPage: true, animations: 'disabled' })
    await page.keyboard.press('Escape')
    assert(await badge.evaluate(el => el === document.activeElement))
    await page.getByRole('button', { name: '检查更新', exact: true }).click()
    await page.getByText(`发现新版本 v${nextVersion}`, { exact: true }).waitFor()
    await page.getByText('本次更新内容', { exact: true }).click()
    const notes = page.locator('.about-notes-content')
    assert.equal(await notes.getByRole('heading', { name: `v${nextVersion} 更新内容`, exact: true }).count(), 1)
    assert.equal(await notes.getByRole('heading', { name: '账号授权', exact: true }).count(), 1)
    assert.equal(await notes.getByRole('listitem').count(), 2)
    assert.equal(await notes.locator('strong').innerText(), 'Google OAuth')
    assert.equal(await notes.locator('code').first().innerText(), 'WorkosCursorSessionToken')
    assert.equal(await notes.locator('pre code').innerText(), 'Cookie -> 桌面凭证\n')
    const notesLink = notes.getByRole('link', { name: '发行记录', exact: true })
    assert.equal(await notesLink.getAttribute('target'), '_blank')
    assert.equal(await notesLink.getAttribute('rel'), 'noopener noreferrer')
    assert.equal(await notes.locator('img').count(), 0)
    assert.equal(await notes.getByRole('link', { name: '不会自动加载的远程图片', exact: true }).getAttribute('href'), 'https://example.test/tracker.png')
    assert.equal(await page.locator('.about-notes script').count(), 0)
    assert.match(await notes.innerText(), /<script>bad\(\)<\/script>/)
    assert.doesNotMatch(await notes.innerText(), /(^|\n)#{1,6} |\*\*|^- /m)
    if (admin) {
      await page.screenshot({ path: join(root, 'output/playwright/update-web.png'), fullPage: true })
      await page.getByRole('button', { name: '一键升级服务器', exact: true }).click()
      await page.getByText('等待开始升级', { exact: false }).waitFor()
      assert.equal(installs, 1)
      assert(await page.getByRole('button', { name: '正在升级…', exact: true }).isDisabled())
      job = { ...job, stage: 'failed', message: '已恢复旧版本和原数据。' }
      await page.getByText('升级未完成', { exact: false }).waitFor()
    } else {
      assert.equal(await page.getByRole('button', { name: '一键升级服务器' }).count(), 0)
      await page.getByText('请联系实例管理员升级服务器。').waitFor()
    }
    await page.setViewportSize({ width: 390, height: 844 })
    await badge.click(); await popup.waitFor()
    const menuBounds = await popup.boundingBox()
    assert(menuBounds.x >= 0 && menuBounds.x + menuBounds.width <= 390)
    await page.keyboard.press('Escape')
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await context.close()
  }
  const context = await browser.newContext({ viewport: { width: 1180, height: 780 } })
  await context.addInitScript(({ current, latest }) => {
    localStorage.setItem('cursor.v2.onboarding', JSON.stringify({ version: 1, seen: true, active: false, step: 0 }))
    globalThis.isTauri = true
    window.updateFixture = { release: latest, failCheck: false, installs: 0, checks: 0, channel: null }
    let callback = 0
    window.__TAURI_INTERNALS__ = {
      transformCallback: () => ++callback, unregisterCallback: () => {},
      invoke: async (command, args) => {
        if (command === 'check_update') { window.updateFixture.checks++; if (window.updateFixture.failCheck) throw '无法连接发布服务，请稍后重试。'; return window.updateFixture.release }
        if (command === 'install_update') {
          window.updateFixture.installs++
          window.updateFixture.channel = args.progress
          args.progress.onmessage({ stage: 'downloading', downloaded: 50, total: 100 })
          return new Promise((resolve, reject) => { window.updateFixture.reject = reject; window.updateFixture.resolve = resolve })
        }
        if (command === 'desktop_request') return { status: 200, body: { phase: 'ready', background: false } }
        if (command === 'connection_request') return { status: 200, body: { active_id: null, generation: 0, items: [] } }
        if (command === 'account_request') return { status: 200, body: args.operation === 'bootstrap'
          ? { mode: 'local', initialized: true, api_version: 1, app_version: current, capabilities: {} }
          : { id: 'native-user', login: '本地用户', instance_admin: false, workspaces: [{ id: 'native-space', name: '本地', kind: 'personal', role: 'owner', capabilities: {} }] } }
        if (command === 'frontend_ready') return null
        throw Error('Unexpected native command: ' + command)
      },
    }
  }, { current, latest })
  const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message))
  await page.clock.install()
  await page.goto(origin + '/#/about'); await page.getByTestId('app-version').waitFor()
  await page.locator('.version-badge.has-update').waitFor()
  assert.equal(await page.evaluate(() => window.updateFixture.checks), 1)
  await page.clock.fastForward(4 * 60 * 60 * 1000 - 10000)
  assert.equal(await page.evaluate(() => window.updateFixture.checks), 1)
  await page.clock.fastForward(11000)
  await page.waitForFunction(() => window.updateFixture.checks === 2)
  assert.equal(await page.evaluate(() => window.updateFixture.installs), 0, 'Scheduled checks never start installation')
  await context.setOffline(true)
  await page.clock.fastForward(4 * 60 * 60 * 1000 + 1000)
  assert.equal(await page.evaluate(() => window.updateFixture.checks), 2)
  await context.setOffline(false)
  await page.clock.runFor(100)
  await page.waitForFunction(() => window.updateFixture.checks === 3)
  await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
  await page.locator('.sidebar.collapsed .version-badge').click()
  await page.getByRole('dialog', { name: '版本与更新', exact: true }).waitFor()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '展开侧栏', exact: true }).click()
  await page.evaluate(() => { window.updateFixture.failCheck = true })
  await page.getByRole('button', { name: '检查更新', exact: true }).click()
  await page.getByRole('alert').filter({ hasText: '无法连接发布服务' }).waitFor()
  assert(await page.locator('.version-badge').evaluate(el => el.classList.contains('has-update')), 'A failed check preserves the known update notice')
  await page.evaluate(() => { window.updateFixture.failCheck = false })
  await page.getByRole('button', { name: '检查更新', exact: true }).click()
  await page.getByText('本次更新内容', { exact: true }).click()
  await page.locator('.about-notes-content').getByRole('heading', { name: `v${nextVersion} 更新内容`, exact: true }).waitFor()
  await page.getByRole('button', { name: '一键升级并重启', exact: true }).click()
  await page.getByText('正在下载 50%', { exact: true }).waitFor()
  assert(await page.getByRole('button', { name: '正在升级…', exact: true }).isDisabled())
  await page.screenshot({ path: join(root, 'output/playwright/update-desktop.png'), fullPage: true })
  await page.evaluate(() => { window.updateFixture.reject('下载或签名校验失败，当前应用未被替换，请重试。') })
  await page.getByRole('alert').filter({ hasText: '签名校验失败' }).waitFor()
  assert.equal(await page.evaluate(() => window.updateFixture.installs), 1)
  assert(await page.getByRole('button', { name: '一键升级并重启', exact: true }).isEnabled())
  await page.evaluate(() => { window.updateFixture.release = { ...window.updateFixture.release, available: false, installable: false, latest_version: window.updateFixture.release.current_version } })
  await page.getByRole('button', { name: '检查更新', exact: true }).click()
  await page.getByText('当前已是最新版本', { exact: true }).waitFor()
  assert.equal(await page.locator('.version-update-dot').count(), 0)
  await page.locator('.version-badge').click()
  const currentPopup = page.getByRole('dialog', { name: '版本与更新', exact: true })
  await currentPopup.getByText('已是最新版本', { exact: true }).waitFor()
  await page.screenshot({ path: join(root, 'output/playwright/version-badge-current.png'), fullPage: true, animations: 'disabled' })
  await page.keyboard.press('Escape')
  assert.equal(await page.getByRole('button', { name: '一键升级并重启' }).count(), 0)
  assert.deepEqual(errors, [])
  console.log('PASS updates: brand badge, breathing/reduced motion, popover/focus/mobile/collapse, four-hour schedule and cache, Web/admin permissions, install progress and retry.')
} finally { await browser.close(); server.close() }
