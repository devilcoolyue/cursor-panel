import type { components } from './api.generated'
import { DesktopError, isDesktop, localRequest } from './platform'
export type Schema = components['schemas']
export type Account = Schema['AccountView']
export type Workspace = Schema['WorkspaceView']
export type Me = Schema['Me']

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

const cookieErrors = new Map<string, string>([
  ['invalid_session_cookie', 'Cookie 格式无效或已过期，请重新登录 Cursor 网页版并复制完整的会话 Cookie。'],
  ['cookie_account_mismatch', 'Cookie 的账号与 Token 不一致，请重新复制同一账号的完整会话 Cookie。'],
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
        const cookieMessage = response.status === 422 && typeof data?.code === 'string' ? cookieErrors.get(data.code) : undefined
        throw new ApiError(response.status, cookieMessage || messages[response.status] || '请求失败，请稍后重试。')
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
