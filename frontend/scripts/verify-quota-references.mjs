import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

export async function verifyQuotaReferences(page) {
  // The synthetic browser confirms as the exhausted-quota subject named in the label.
  const label = '额度参考测试 user_quota_capped'
  await page.getByRole('button', { name: /添加账号/ }).click()
  await page.getByLabel('账号名称', { exact: true }).fill(label)
  await page.getByRole('button', { name: '在浏览器中授权', exact: true }).click()
  const card = page.locator('[data-account]').filter({ has: page.getByRole('heading', { name: label, exact: true }) })
  await card.waitFor()
  const statsBefore = await card.locator('.card-meta dd').first().getAttribute('title')
  async function edit() {
    await card.getByRole('button', { name: '更多账号操作', exact: true }).click()
    await card.getByRole('button', { name: '编辑资料与标签', exact: true }).click()
    await page.getByText('额度上限参考值（可选）', { exact: true }).click()
  }
  async function saveEdit() {
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await page.getByRole('dialog', { name: '编辑账号', exact: true }).waitFor({ state: 'detached' })
    // Saving closes the dialog before the follow-up list request finishes.
    // Wait for that request and its Vue render before inspecting saved card values.
    await page.locator('.account-grid[aria-busy="false"]').waitFor()
  }
  await edit()
  await page.getByLabel('Cursor Models 参考上限', { exact: true }).fill('450')
  await page.getByLabel('Other Models 参考上限', { exact: true }).fill('45')
  await page.getByLabel('综合 参考上限', { exact: true }).fill('400')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await page.getByText('综合参考上限应等于两个模型分池之和。', { exact: true }).waitFor()
  await page.getByLabel('综合 参考上限', { exact: true }).fill('495')
  const platform = await page.evaluate(() => window.isTauri ? 'desktop' : 'web')
  await page.screenshot({ path: fileURLToPath(new URL(`../../output/playwright/${platform}-quota-reference-edit.png`, import.meta.url)), animations: 'disabled' })
  await saveEdit()
  assert.equal(await card.locator('.card-meta dd').first().getAttribute('title'), statsBefore)
  await card.getByRole('button', { name: '刷新', exact: true }).click()
  await card.locator('.card-refresh-skeleton').waitFor({ state: 'detached' })
  const labels = await card.locator('.quota-limit').allTextContents()
  for (const [index, value] of ['$450', '$45', '$495'].entries()) assert.ok(labels[index].includes(value))
  assert.equal(await card.getByText('剩 0%', { exact: true }).count(), 3)
  assert.equal(await card.locator('.quota-limit').last().getAttribute('title'), '本账号本账期手动填写的参考上限，仅供参考；剩余百分比仍按 Cursor 返回值显示。账期或套餐变化后自动失效。')
  assert.match(await card.locator('.spend-limit').innerText(), /495/)
  assert.match(await card.locator('.card-meta .danger-text').innerText(), /496.15/)
  await page.screenshot({ path: fileURLToPath(new URL(`../../output/playwright/${platform}-quota-reference-card.png`, import.meta.url)), animations: 'disabled' })
  await edit()
  assert.equal(await page.getByLabel('综合 参考上限', { exact: true }).inputValue(), '495')
  for (const field of ['Cursor Models', 'Other Models', '综合']) await page.getByLabel(`${field} 参考上限`, { exact: true }).fill('')
  await saveEdit()
  assert.ok(!(await card.locator('.quota-limit').last().getAttribute('title')).includes('手动填写'))
  await card.getByRole('button', { name: '更多账号操作', exact: true }).click()
  await card.getByRole('button', { name: '删除账号', exact: true }).click()
  await page.getByRole('button', { name: '确认删除', exact: true }).click()
  await card.waitFor({ state: 'detached' })
  console.log('PASS quota references: validation, saved limits on exhausted cards, preserved percentages/timestamps, provider refresh, edit and clear.')
}
