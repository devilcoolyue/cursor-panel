/** Shared Web/native-bridge regression: browser confirmation against the synthetic Cursor provider. */
import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'

/** The synthetic browser signs in as the subject named in the account label (see PreviewGateway). */
export async function verifyBrowserAuthorization(page, logins) {
  async function authorize(form) {
    const before = logins.length
    await form.getByRole('button', { name: '在浏览器中授权', exact: true }).click()
    for (let waited = 0; logins.length === before; waited += 50) {
      assert.ok(waited < 15000, 'Cursor login page was not opened')
      await delay(50)
    }
    const url = new URL(logins.at(-1))
    assert.equal(url.origin + url.pathname, 'https://cursor.com/loginDeepControl')
    assert.deepEqual([...url.searchParams.keys()].sort(), ['challenge', 'mode', 'uuid'])
  }
  await page.getByRole('button', { name: '＋ 添加账号', exact: true }).click()
  const form = page.getByRole('dialog', { name: '添加 Cursor 账号', exact: true })
  assert.equal(await form.locator('textarea').count(), 0)
  const label = 'OAuth 验证 google-oauth2|user_oauth_import'
  await form.getByLabel('账号名称').fill(label)
  await authorize(form)
  await form.getByText('等待在浏览器中确认', { exact: true }).waitFor()
  assert.equal(await form.getByRole('button', { name: '等待确认…', exact: true }).isDisabled(), true)
  await form.waitFor({ state: 'detached' })
  const card = page.locator('[data-account]').filter({ has: page.getByRole('heading', { name: label, exact: true }) })
  await card.waitFor({ state: 'visible' })
  assert.match(await card.innerText(), /oauth_import@example\.test/)
  // A browser signed in as another Cursor account cannot replace this account.
  await card.getByLabel('更多账号操作').click()
  await card.getByRole('button', { name: '重新授权', exact: true }).click()
  const authorization = page.getByRole('dialog', { name: '重新授权账号', exact: true })
  await authorization.getByLabel('账号名称').fill('OAuth 验证 user_other')
  await authorize(authorization)
  await authorization.getByRole('alert').waitFor({ state: 'visible' })
  assert.equal(await authorization.getByRole('alert').innerText(), '浏览器中确认的 Cursor 账号与要重新授权的账号不一致，请在浏览器切换到该账号后重试。')
  await authorization.getByLabel('账号名称').fill(label)
  await authorize(authorization)
  await authorization.waitFor({ state: 'detached' })
  await card.getByLabel('更多账号操作').click()
  await card.getByRole('button', { name: '删除账号', exact: true }).click()
  await page.getByRole('button', { name: '确认删除', exact: true }).click()
  await card.waitFor({ state: 'detached' })
}
