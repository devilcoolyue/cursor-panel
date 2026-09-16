/** Shared Web/native-bridge regression using synthetic OAuth credentials only. */
import assert from 'node:assert/strict'

export async function verifyCookieAuthorization(page) {
  const cookie = (subject, exp = Math.floor(Date.now() / 1000) + 86400) => {
    const claims = Buffer.from(JSON.stringify({ sub: subject, type: 'web', exp })).toString('base64url')
    return `user_oauth_import::eyJhbGciOiJIUzI1NiJ9.${claims}.synthetic`
  }
  await page.getByRole('button', { name: '＋ 添加账号', exact: true }).click()
  const form = page.getByRole('dialog', { name: '添加 Cursor 账号', exact: true })
  const input = form.getByLabel('Cursor 网页会话 Cookie')
  await form.getByLabel('账号名称').fill('OAuth 验证')
  for (const [value, message] of [
    ['synthetic-invalid-cookie', 'Cookie 格式无效或已过期，请重新登录 Cursor 网页版并复制完整的会话 Cookie。'],
    [cookie('google-oauth2|user_oauth_import', 1), 'Cookie 格式无效或已过期，请重新登录 Cursor 网页版并复制完整的会话 Cookie。'],
    [cookie('google-oauth2|user_other'), 'Cookie 的账号与 Token 不一致，请重新复制同一账号的完整会话 Cookie。'],
  ]) {
    await input.fill(value)
    await form.getByRole('button', { name: '保存', exact: true }).click()
    await form.getByRole('alert').waitFor({ state: 'visible' })
    assert.equal(await form.getByRole('alert').innerText(), message)
    assert.equal(await input.inputValue(), '')
  }
  const value = cookie('google-oauth2|user_oauth_import')
  await input.fill(value)
  await form.getByRole('button', { name: '保存', exact: true }).click()
  await form.waitFor({ state: 'detached' })
  const card = page.locator('[data-account]').filter({ has: page.getByRole('heading', { name: 'OAuth 验证', exact: true }) })
  await card.waitFor({ state: 'visible' })
  assert.match(await card.innerText(), /oauth_import@example\.test/)
  await card.getByLabel('更多账号操作').click()
  await card.getByRole('button', { name: '重新授权', exact: true }).click()
  const authorization = page.getByRole('dialog', { name: '重新授权账号', exact: true })
  await authorization.getByLabel('Cursor 网页会话 Cookie').fill(value)
  await authorization.getByRole('button', { name: '保存', exact: true }).click()
  await authorization.waitFor({ state: 'detached' })
  await card.getByLabel('更多账号操作').click()
  await card.getByRole('button', { name: '删除账号', exact: true }).click()
  await page.getByRole('button', { name: '确认删除', exact: true }).click()
  await card.waitFor({ state: 'detached' })
}
