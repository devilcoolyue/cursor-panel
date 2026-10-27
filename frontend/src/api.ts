import type { components } from './api.generated'
import { DesktopError, isDesktop, localRequest } from './platform'
export type Schema = components['schemas']
export type Account = Schema['AccountView']
export type Workspace = Schema['WorkspaceView']
export type Me = Schema['Me']
export type AuthorizationStarted = Schema['AuthorizationStarted']
export type AuthorizationProgress = Schema['AuthorizationProgress']

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

const errorCodes = new Map<string, string>([
  ['authorization_expired', '授权已超时，请重新在浏览器中授权。'],
  ['authorization_account_mismatch', '浏览器中确认的 Cursor 账号与要重新授权的账号不一致，请在浏览器切换到该账号后重试。'],
  ['account_exists', '该 Cursor 账号已在当前空间中，请在它的账号卡片上使用「重新授权」。'],
])

/** One in-memory request scope per user/workspace. Even non-abortable responses are discarded. */
export class ApiClient {
  csrf = ''
  private generation = 0
  private controllers = new Set<AbortController>()
  onUnauthorized = () => {}
  invalidate() {
    this.generation++
    for (const controller of this.controllers) controller.abort()
    this.controllers.clear()
  }
  async request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    const generation = this.generation
    const controller = new AbortController()
    this.controllers.add(controller)
    const abort = () => controller.abort()
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) controller.abort()
    try {
      const response = isDesktop ? await localRequest(path, method, body) : await fetch(`/api/v1${path}`, {
        method, credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal: controller.signal,
        headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
          ...(method !== 'GET' ? { 'X-CSRF-Token': this.csrf } : {}) },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      })
      const data = response.status === 204 ? undefined : await response.json()
      if (generation !== this.generation || controller.signal.aborted) throw new DOMException('Obsolete response', 'AbortError')
      if (!response.ok) {
        if (response.status === 401 && path !== '/auth/login' && path !== '/invitations/accept') this.onUnauthorized()
        const messages: Record<number, string> = { 401: '登录已失效，请重新登录。', 403: '当前没有此操作权限，请重新载入权限。',
          404: '内容不可用，可能已删除或收回授权。', 409: '状态已发生变化，请重新载入后重试。', 422: '请检查填写的内容。',
          426: '实例协议不兼容，请升级服务端或桌面应用。', 429: '操作过于频繁，请稍后重试。', 502: '服务请求失败，请检查网络后重试。', 503: '服务暂不可用，请联系实例管理员。' }
        // Only recognized codes select public messages; never render upstream detail or credentials.
        const codeMessage = typeof data?.code === 'string' ? errorCodes.get(data.code) : undefined
        throw new ApiError(response.status, codeMessage || messages[response.status] || '请求失败，请稍后重试。')
      }
      return data as T
    } finally {
      this.controllers.delete(controller)
      signal?.removeEventListener('abort', abort)
    }
  }
}
export const api = new ApiClient()
export const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError'
export const message = (error: unknown) => isAbort(error) ? '' : error instanceof ApiError || error instanceof DesktopError ? error.message : '无法连接服务，请检查网络后重试。'
export const spacePath = (id: string) => `/workspaces/${encodeURIComponent(id)}`
export const accountPath = (account: Account) => `${spacePath(account.workspace_id)}/accounts/${encodeURIComponent(account.id)}`
