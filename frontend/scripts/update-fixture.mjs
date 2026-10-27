import { readFileSync } from 'node:fs'

const version = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version
export const currentRelease = { current_version: version, latest_version: version, available: false, installable: false,
  notes: '', release_url: 'https://github.com/devilcoolyue/cursor-panel/releases', published_at: null }

/** Keep automatic checks in unrelated browser fixtures offline and deterministic. */
export async function fixtureContext(browser, options) {
  const context = await browser.newContext(options)
  await context.route('**/api/v1/updates', route => route.fulfill({ json: currentRelease }))
  // Browser authorization opens Cursor's confirmation page; record and close it without contacting cursor.com.
  context.cursorLogins = []
  await context.route('https://cursor.com/**', route => route.fulfill({ contentType: 'text/html', body: '<title>Cursor login fixture</title>' }))
  context.on('page', page => page.waitForURL(/^https:\/\/cursor\.com\/loginDeepControl\?/, { timeout: 15000 })
    .then(() => { context.cursorLogins.push(page.url()); return page.close() }).catch(() => {}))
  return context
}
