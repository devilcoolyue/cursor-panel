import { invoke, isTauri } from '@tauri-apps/api/core'
import { ref } from 'vue'

export const isDesktop = isTauri()
export const connectionId = ref<string | null>(null)
export interface InstanceConnection { id: string; name: string; origin: string; device_id: string; phase: string }
export interface ConnectionsState { active_id: string | null; generation: number; items: InstanceConnection[]; revoked?: boolean }
export interface NativeResponse<T = unknown> { status: number; body: T }
export interface SwitchState { stage: string; busy: boolean; backup_id: string | null; error: string | null; written?: boolean }
export interface DesktopStatus {
  phase: 'starting' | 'ready' | 'locked' | 'in_use' | 'unavailable'
  error?: string | null; background: boolean; api_version?: number
  last_refresh?: number | null; refresh_error?: string | null; switch?: SwitchState
}
export interface Detection {
  platform: string; available: boolean; running: boolean; reason: string | null
  executable_path?: string | null; user_data_path?: string | null; database_path?: string | null
  source?: string | null; configured_executable_path?: string; configured_user_data_path?: string; saved?: boolean
}
export class DesktopError extends Error {}
export function desktopMessage(status: number) {
  return ({ 409: '操作未完成。请检查目标状态；导入需要空的个人空间，导出请使用新的文件名。',
    423: '本地密钥不可用或归档口令不正确，请解锁系统凭证库或使用正确的加密归档恢复。',
    422: '请检查填写的内容。', 401: '设备登录已到期或撤销，请在系统浏览器中重新登录。',
    403: '当前没有此操作权限。', 426: '实例协议不兼容，请升级服务端或桌面应用。',
    502: '无法连接远程实例，请检查地址与网络后重试。', 503: '远程实例暂不可用。' } as Record<number, string>)[status] || '本地操作失败，请重试。'
}
async function checked<T>(command: string, args: Record<string, unknown>): Promise<T> {
  if (!isDesktop) throw new DesktopError('请在 Cursor Panel 桌面应用中执行。')
  let result: NativeResponse<T>
  try { result = await invoke<NativeResponse<T>>(command, args) }
  catch { throw new DesktopError('本地后台连接中断，请重新打开 Cursor Panel。') }
  if (result.status >= 400) throw new DesktopError(desktopMessage(result.status))
  return result.body
}
export const native = <T>(operation: 'status' | 'unlock' | 'detect' | 'cursor_paths' | 'switch' | 'switch_command' | 'switch_status' | 'backups' | 'restore' | 'background' | 'resume', body?: unknown) =>
  checked<T>('desktop_request', { operation, body })
export const archive = (operation: 'export' | 'import' | 'recover', password: string, workspace?: string) =>
  checked<{ count?: number; cancelled?: boolean; phase?: string }>('desktop_archive', { operation, password, workspace })
export async function openBackups() { await invoke('desktop_open_backups') }
/** Native code opens only Cursor's login confirmation page in the system browser. */
export async function openCursorLogin(url: string) { await invoke('open_cursor_login', { url }) }
export const connectionAction = (operation: 'list' | 'add' | 'select' | 'login' | 'disconnect' | 'remove', id?: string, body?: unknown) =>
  checked<ConnectionsState>('connection_request', { operation, connection_id: id, body })

/** Translate shared business calls into an enum, UUIDs and fields. Rust builds the final URL. */
export async function localRequest(path: string, method: string, body?: unknown): Promise<Response> {
  const url = new URL(path, 'http://local.invalid')
  const parts = url.pathname.split('/').filter(Boolean)
  let operation = '', workspace: string | undefined, account: string | undefined, authorization: string | undefined
  if (parts.length === 1 && method === 'GET' && ['bootstrap', 'me'].includes(parts[0]!)) operation = parts[0]!
  else if (parts[0] === 'workspaces' && parts[1]) {
    workspace = parts[1]
    if (parts[2] === 'audit' && parts.length === 3 && method === 'GET') operation = 'audit'
    else if (parts[2] === 'accounts') {
      account = parts[3]
      if (parts.length === 3 && method === 'GET') operation = 'list'
      if (parts.length === 4) operation = ({ GET: 'get', PATCH: 'edit', DELETE: 'delete' } as Record<string, string>)[method] || ''
      if (parts.length === 5) operation = ({ 'POST:refresh': 'refresh', 'GET:detail': 'detail' } as Record<string, string>)[`${method}:${parts[4]}`] || ''
    } else if (parts[2] === 'authorizations') {
      authorization = parts[3]
      if (parts.length === 3 && method === 'POST') operation = 'authorize'
      if (parts.length === 4 && method === 'DELETE') operation = 'authorization_cancel'
      if (parts.length === 5 && method === 'POST' && parts[4] === 'poll') operation = 'authorization_poll'
    }
  }
  if (!operation && connectionId.value) return remoteManage(parts, method, body, url.searchParams)
  if (!operation) throw new DesktopError('此功能暂不适用于独立桌面。')
  const query = Object.fromEntries([...url.searchParams].map(([key, value]) => [key, ['limit', 'offset'].includes(key) ? Number(value) : value]))
  const result = await invoke<NativeResponse>('account_request', { operation, workspace, account, authorization, query, body, connection_id: connectionId.value })
  return new Response(result.status === 204 ? null : JSON.stringify(result.body), { status: result.status, headers: { 'Content-Type': 'application/json' } })
}

async function remoteManage(parts: string[], method: string, body: unknown, query: URLSearchParams): Promise<Response> {
  let operation = '', workspace: string | undefined, account: string | undefined, target: string | undefined
  if (parts[0] === 'auth') {
    target = parts[2]
    operation = ({ 'GET:sessions': 'sessions', 'DELETE:sessions': 'revoke_session', 'GET:devices': 'devices',
      'DELETE:devices': 'revoke_device', 'PUT:password': 'password' } as Record<string, string>)[`${method}:${parts[1]}`] || ''
  } else if (parts[0] === 'instance') {
    target = parts[2]
    operation = ({ 'GET:users': 'users', 'PUT:users': 'user_state', 'GET:audit': 'instance_audit' } as Record<string, string>)[`${method}:${parts[1]}`] || ''
  } else if (parts[0] === 'workspaces') {
    workspace = parts[1]
    if (parts.length === 1 && method === 'POST') operation = 'create_workspace'
    if (parts.length === 2 && method === 'DELETE') operation = 'delete_workspace'
    if (parts[2] === 'accounts' && parts[4] === 'grants') {
      account = parts[3]; target = parts[5]
      operation = ({ GET: 'grants', PUT: 'grant', DELETE: 'revoke_grant' } as Record<string, string>)[method] || ''
    } else {
      target = parts[3]
      operation ||= ({ 'GET:members': 'members', 'PUT:members': 'role', 'DELETE:members': 'remove_member',
        'GET:invitations': 'invitations', 'POST:invitations': 'invite', 'DELETE:invitations': 'revoke_invitation',
        'PUT:owner': 'transfer_owner' } as Record<string, string>)[`${method}:${parts[2]}`] || ''
    }
  }
  if (!operation) throw new DesktopError('此远程操作暂不可用，请在实例网页中执行。')
  const result = await invoke<NativeResponse>('remote_manage', { operation, connection_id: connectionId.value,
    workspace, account, target, offset: query.has('offset') ? Number(query.get('offset')) : undefined, body })
  return new Response(result.status === 204 ? null : JSON.stringify(result.body), { status: result.status, headers: { 'Content-Type': 'application/json' } })
}

export interface Probe {
  backend: { api_version: number; frozen: boolean; python: string; sqlite: string; pid: number }
  accounts: { id: string; label: string; email: string; remaining_pct: number }[]
  fixture: string
  keyring: { ok: boolean; detail: string }
  backend_start_ms: number
}

export async function probe(): Promise<Probe> {
  if (!isTauri()) throw new Error('请使用桌面验证程序运行；浏览器预览不连接本机后台。')
  return invoke<Probe>('probe')
}

export async function reportReady(renderedAccounts: number): Promise<void> {
  if (isTauri()) await invoke('frontend_ready', { renderedAccounts, title: document.title })
}
