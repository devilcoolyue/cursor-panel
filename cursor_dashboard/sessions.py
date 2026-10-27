"""Desktop credential exchange/refresh shared by the web server and CLI."""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
from urllib.parse import urlencode

import requests

from . import store
from .client import AuthExpired
from .config import TOKEN_REFRESH_MARGIN
from .desktop import DesktopSession, DesktopSessionError, desktop_session, refreshed_session, token_claims

# 浏览器确认页由用户在自己的浏览器里提交，Cursor 的 CSRF 与风控校验都在那里完成；
# 面板不再在浏览器外提交网页回调（Castle 风控令牌只能由该页面生成）。
LOGIN_URL = "https://cursor.com/loginDeepControl"
SUBJECT = re.compile(r"(?:[A-Za-z0-9_-]+\|)?user_[A-Za-z0-9_-]+")


def session_from_account(account):
    return DesktopSession(account["access_token"], account["auth_subject"], account["token_expires_at"],
                          account["refresh_token"], "session")


def verify_identity(data, *, email=None, subject=None):
    actual_email = data.get("email") if isinstance(data, dict) else None
    actual_subject = (data.get("authId") or data.get("sub")) if isinstance(data, dict) else None
    if not isinstance(actual_email, str) or not actual_email.strip():
        raise AuthExpired("会话已失效，请重新授权。")
    if email and actual_email.casefold() != email.casefold():
        raise DesktopSessionError("凭证与所选账号的邮箱不一致。")
    if subject and (not isinstance(actual_subject, str)
                    or actual_subject.removeprefix("auth0|") != subject.removeprefix("auth0|")):
        raise DesktopSessionError("凭证与所选账号的身份不一致。")
    return actual_email


def login_url(flow, challenge):
    return LOGIN_URL + "?" + urlencode({"challenge": challenge, "uuid": flow, "mode": "login"})


async def poll_session(fetch, label, flow, verifier):
    """浏览器确认后返回桌面会话；尚未确认（上游 404）时返回 None。"""
    try:
        data = await fetch("", label, "desktop_poll", flow, verifier)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    subject = token_claims(data.get("accessToken") if isinstance(data, dict) else None).get("sub")
    if not isinstance(subject, str) or not SUBJECT.fullmatch(subject):
        raise DesktopSessionError("Cursor 返回的桌面凭证身份无效。")
    return desktop_session(data, subject)


def _usable(account, rejected_token):
    lifetime = account.get("token_expires_at", 0) - account.get("auth_refreshed_at", 0)
    margin = min(TOKEN_REFRESH_MARGIN, max(30, lifetime * .2))
    return (account.get("access_token") and account.get("refresh_token") and not account.get("auth_invalid")
            and account.get("token_expires_at", 0) > time.time() + margin
            and (rejected_token is None or account["access_token"] != rejected_token))


async def ensure_account(account, fetch, *, rejected_token=None):
    """Reuse credentials, or exclusively renew/migrate them without holding a SQLite transaction."""
    key = store.account_id(account)
    owner = secrets.token_hex(16)
    deadline = time.monotonic() + 180
    while True:
        current = await asyncio.to_thread(store.get_account, key)
        if current is None:
            raise store.AccountsError("账号已被删除。")
        if current.get("auth_invalid"):
            raise AuthExpired("桌面会话已被撤销，请重新授权。")
        if _usable(current, rejected_token):
            return current
        if await asyncio.to_thread(store.claim_auth_lease, key, owner):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("账号凭证正在更新，请稍后重试。")
        await asyncio.sleep(.15)
    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(store.renew_auth_lease, key, owner)

    keepalive = asyncio.create_task(heartbeat())
    try:
        current = await asyncio.to_thread(store.get_account, key)
        if current is None:
            raise store.AccountsError("账号已被删除。")
        if _usable(current, rejected_token):
            return current
        if current.get("auth_invalid"):
            raise AuthExpired("桌面会话已被撤销，请重新授权。")
        label = current.get("label") or key
        if not current.get("refresh_token"):
            # Cursor 不再接受在浏览器外用 Cookie 换取桌面凭证。
            raise AuthExpired("旧账号需要在浏览器中重新授权。")
        try:
            data = await fetch("", label, "desktop_refresh", current["refresh_token"])
            if isinstance(data, dict) and data.get("shouldLogout") is True:
                raise AuthExpired("桌面会话已被撤销，请重新授权。")
            session = refreshed_session(data, current["auth_subject"])
        except AuthExpired:
            changed = await asyncio.to_thread(store.update_session, current, invalid=True)
            if not changed:
                return await _latest(key)
            raise
        await asyncio.to_thread(store.update_session, current, session)
        return await _latest(key)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await keepalive
        await asyncio.to_thread(store.release_auth_lease, key, owner)


async def _latest(key):
    latest = await asyncio.to_thread(store.get_account, key)
    if latest is None:
        raise store.AccountsError("账号已被删除。")
    if latest.get("auth_invalid") or not latest.get("refresh_token"):
        raise AuthExpired("桌面授权已变更，请重新授权。")
    return latest


async def request_account(account, fetch, name, *args):
    current = await ensure_account(account, fetch)
    label = current.get("label") or store.account_id(current)
    try:
        return await fetch("", label, name, current["access_token"], *args)
    except AuthExpired:
        current = await ensure_account(current, fetch, rejected_token=current["access_token"])
        return await fetch("", label, name, current["access_token"], *args)
