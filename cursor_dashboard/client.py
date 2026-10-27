"""Cursor 网页授权与 api2.cursor.sh 桌面内部接口的薄封装。

这些接口不是公开 API 契约，字段随时可能变。
初次授权使用 WorkosCursorSessionToken；常规请求使用桌面 Bearer 凭证。
"""

from __future__ import annotations

import random
import time

import requests

from .config import (
    RATE_LIMIT_BASE_DELAY,
    RATE_LIMIT_RETRIES,
    REQUEST_RETRIES,
    RETRY_BASE_DELAY,
)

BASE = "https://cursor.com"
DESKTOP_BASE = "https://api2.cursor.sh"
COOKIE_NAME = "WorkosCursorSessionToken"
# cursor.com 的 POST 接口使用双提交 CSRF：Cookie 与请求头必须是同一个值
CSRF_COOKIE = "csrf-token"
CSRF_HEADER = "x-csrf-token"
TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 保留的旧网页接口集合；当前面板和 CLI 使用下面的 DESKTOP_ENDPOINTS。
ENDPOINTS = ("me", "plan_info", "usage_summary", "period_usage", "grok_status")
# 常规桌面取数为 6 项；模型明细按需查询，不加入后台刷新集合。
DESKTOP_ENDPOINTS = ("desktop_me", "desktop_plan", "desktop_profile", "desktop_period", "desktop_grok", "desktop_limit")
AUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
RETRYABLE_STATUS = {500, 502, 504}
# 被挡住时的状态码。429 是标准限流；403 只有在返回 HTML 时才算（见 _call）；
# 503 通常是边缘节点在挡，不是接口真的挂了。
THROTTLE_STATUS = {429, 503}
RETRY_AFTER_CAP = 120


class AuthExpired(RuntimeError):
    """远端拒绝认证；调用方据凭证类型决定续期或要求重新授权。"""


class RateLimited(RuntimeError):
    """被 Cursor / Vercel 临时限制，退避后重试；不能据此判定凭证失效。

    **不要把它并进 AuthExpired**：限流返回的是 403 + HTML 安全拦截页，
    过去一律当成失效，结果整屏卡片变红、用户重新粘贴 cookie 还是红的。
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(r: requests.Response) -> float | None:
    """解析 Retry-After（只认整秒写法，HTTP-date 少见就不猜了）。"""
    raw = (r.headers.get("Retry-After") or "").strip()
    if not raw.isdigit():
        return None
    return min(float(raw), RETRY_AFTER_CAP)


def _is_json(r: requests.Response) -> bool:
    return "json" in (r.headers.get("Content-Type") or "").lower()


class CursorClient:
    def __init__(self, cookie: str, label: str = ""):
        self.label = label or "unnamed"
        self.s = requests.Session()
        if cookie:
            self.s.cookies.set(COOKIE_NAME, cookie, domain="cursor.com", path="/")
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": f"{BASE}/dashboard/spending",
        })

    def _call(self, method: str, path: str, payload=None, *, base=BASE, params=None, headers=None, read_json=True):
        # 不跟跳转：cookie 失效时接口不返回 401，而是 307 去 WorkOS 登录页，
        # 跟过去只会拿到一个和本次请求无关的 404
        r = self.s.request(method, base + path, json=payload, params=params, headers=headers, timeout=TIMEOUT,
                           allow_redirects=False)
        if r.status_code == 401:
            raise AuthExpired(f"[{self.label}] 会话已失效，请重新登录")
        if r.status_code == 403:
            # 鉴权接口拒绝会带 JSON body；HTML 是边缘防护的拦截页，跟 cookie 无关
            if _is_json(r):
                raise AuthExpired(f"[{self.label}] 会话已失效，请重新登录")
            raise RateLimited(
                f"[{self.label}] Cursor 暂时限制了请求，稍后会自动重试",
                _retry_after(r),
            )
        if r.status_code in THROTTLE_STATUS:
            raise RateLimited(
                f"[{self.label}] Cursor 暂时限制了请求，稍后会自动重试",
                _retry_after(r),
            )
        if r.is_redirect:
            loc = r.headers.get("Location", "")
            if any(h in loc for h in ("/api/auth/login", "workos.com", "/login")):
                raise AuthExpired(f"[{self.label}] 会话已失效，请重新登录")
            raise RuntimeError(f"[{self.label}] {path} 意外跳转: {loc}")
        r.raise_for_status()
        return r.json() if r.content and read_json else {}

    def me(self):             return self._call("GET",  "/api/auth/me")

    def _csrf_token(self) -> str:
        """与网页一致：/api/csrf-token 返回 token 并下发同名 Cookie，POST 再把它放进请求头。
        缺少任一项时上游返回 403 {"error":"Invalid CSRF token"}。"""
        data = self._call("GET", "/api/csrf-token")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"[{self.label}] Cursor 未返回 CSRF token")
        self.s.cookies.set(CSRF_COOKIE, token, domain="cursor.com", path="/")
        return token

    def desktop_callback(self, flow: str, challenge: str):
        return self._call("POST", "/api/auth/loginDeepCallbackControl", {
            "uuid": flow, "challenge": challenge,
        }, headers={CSRF_HEADER: self._csrf_token()}, read_json=False)

    def desktop_poll(self, flow: str, verifier: str):
        return self._call("GET", "/auth/poll", base=DESKTOP_BASE,
                          params={"uuid": flow, "verifier": verifier},
                          headers={"x-cursor-client-type": "ide"})

    def desktop_profile(self, token: str):
        return self._call("GET", "/auth/full_stripe_profile", base=DESKTOP_BASE,
                          headers={"Authorization": f"Bearer {token}", "x-cursor-client-type": "ide"})

    def desktop_refresh(self, refresh_token: str):
        return self._call("POST", "/oauth/token", base=DESKTOP_BASE,
                          headers={"x-cursor-client-type": "ide"}, payload={
                              "grant_type": "refresh_token", "client_id": AUTH_CLIENT_ID,
                              "refresh_token": refresh_token,
                          })

    def _rpc(self, token: str, method: str, payload=None):
        return self._call("POST", "/aiserver.v1.DashboardService/" + method, payload or {},
                          base=DESKTOP_BASE, headers={"Authorization": f"Bearer {token}",
                          "x-cursor-client-type": "ide", "Connect-Protocol-Version": "1"})

    def desktop_me(self, token): return self._rpc(token, "GetMe")
    def desktop_plan(self, token): return self._rpc(token, "GetPlanInfo")
    def desktop_period(self, token): return self._rpc(token, "GetCurrentPeriodUsage")
    def desktop_limit(self, token): return self._rpc(token, "GetHardLimit")

    def desktop_grok(self, token):
        try:
            return self._rpc(token, "GetSandUsageStatus")
        except (AuthExpired, RateLimited):
            raise
        except Exception:
            return {}

    def desktop_aggregated(self, token, start_ms, end_ms):
        return self._rpc(token, "GetAggregatedUsageEvents", {
            "teamId": 0, "userId": 0, "startDate": int(start_ms), "endDate": int(end_ms),
        })

    def plan_info(self):      return self._call("POST", "/api/dashboard/get-plan-info", {})
    def usage_summary(self):  return self._call("GET",  "/api/usage-summary")
    def period_usage(self):   return self._call("POST", "/api/dashboard/get-current-period-usage", {})

    def grok_status(self):
        """Grok Bot 的周额度。非关键数据：普通失败吞掉，别让它拖垮整个账号的刷新。

        但 AuthExpired / RateLimited 必须冒泡——调度器靠这两类异常判断该退避还是
        该报失效，吞掉就等于对限流视而不见。
        """
        try:
            return self._call("POST", "/api/dashboard/get-sand-usage-status", {})
        except (AuthExpired, RateLimited):
            raise              # 这两个要冒泡：调度器靠它们判断该退避还是该报失效
        except Exception:
            return {}          # 非关键数据，其它失败就跳过

    def aggregated_usage(self, start_ms: int, end_ms: int):
        """本周期按模型聚合的 token 与花费。窗口是任意的，传账单周期起点就是"本周期"。

        teamId / userId 传 0 表示"就用这个 cookie 对应的个人账号"——实测传 0、传真实
        id、整个字段不传，返回完全一致；但 teamId 传 -1 会被判成"缺 team id"而 401。
        """
        return self._call("POST", "/api/dashboard/get-aggregated-usage-events", {
            "teamId": 0, "userId": 0,
            "startDate": int(start_ms), "endDate": int(end_ms),
        })


def _backoff(base: float, attempt: int) -> float:
    return base * (2 ** attempt) + random.uniform(0, base)


def fetch_one(cookie: str, label: str, name: str, *args):
    """取单个接口。刻意每次新建 Session —— requests.Session 跨线程共享不安全，
    而这几个请求本来就要并发发出去。瞬时错误和限流会退避重试，退避基数不同：
    连接抖动几百毫秒就够，被挡住则要等几秒。"""
    attempts = max(REQUEST_RETRIES, RATE_LIMIT_RETRIES) + 1
    for attempt in range(attempts):
        client = CursorClient(cookie, label)
        try:
            return getattr(client, name)(*args)
        except AuthExpired:
            raise
        except RateLimited as exc:
            if attempt >= RATE_LIMIT_RETRIES:
                raise
            time.sleep(exc.retry_after or _backoff(RATE_LIMIT_BASE_DELAY, attempt))
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            retryable = isinstance(exc, (requests.ConnectionError, requests.Timeout))
            retryable = retryable or status in RETRYABLE_STATUS
            if not retryable or attempt >= REQUEST_RETRIES:
                raise
            time.sleep(_backoff(RETRY_BASE_DELAY, attempt))
        finally:
            client.s.close()

    raise RuntimeError("unreachable")
