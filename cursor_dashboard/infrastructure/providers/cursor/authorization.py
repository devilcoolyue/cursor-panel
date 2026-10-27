"""Cursor authorization protocol; no persistence or runtime configuration."""
from __future__ import annotations
import asyncio
from urllib.parse import urlencode
import requests
from ....client import AuthExpired
from ....desktop import (DesktopSessionError, SUBJECT, USER_ID, desktop_session, login_challenge, parse_session,
                         token_claims)

# The browser confirms this page itself, so Cursor's CSRF and risk checks run
# where they are designed to; the panel never submits the web callback.
LOGIN_URL = "https://cursor.com/loginDeepControl"


def _profile_subject_matches(actual, expected):
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    if actual.removeprefix("auth0|") == expected.removeprefix("auth0|"):
        return True
    # Cursor's web profile may omit the token's OAuth provider prefix. This
    # compatibility is for profile responses only; token subjects stay intact.
    token_subject = SUBJECT.fullmatch(expected)
    return bool(USER_ID.fullmatch(actual) and token_subject and actual == token_subject.group(1))


def verify_identity(data, *, email=None, subject=None):
    actual_email = data.get("email") if isinstance(data, dict) else None
    actual_subject = (data.get("authId") or data.get("sub")) if isinstance(data, dict) else None
    if not isinstance(actual_email, str) or not actual_email.strip():
        raise AuthExpired("会话已失效，请重新登录网页版并更新 Cookie。")
    if email and actual_email.casefold() != email.casefold():
        raise DesktopSessionError("凭证与所选账号的邮箱不一致。")
    if subject and not _profile_subject_matches(actual_subject, subject):
        raise DesktopSessionError("凭证与所选账号的身份不一致。")
    return actual_email


async def exchange_cookie(cookie, label, fetch, *, expected_email=None):
    """Legacy V1 cookie exchange. Cursor's browser risk check now rejects its web callback."""
    source = parse_session(cookie)
    me = await fetch(cookie, label, "me")
    email = verify_identity(me, email=expected_email, subject=source.subject)
    flow, verifier, challenge = login_challenge()
    await fetch(cookie, label, "desktop_callback", flow, challenge)
    for attempt in range(5):
        try:
            data = await fetch("", label, "desktop_poll", flow, verifier)
            session = desktop_session(data, source.subject)
            break
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            if attempt == 4:
                raise DesktopSessionError("Cursor 尚未签发桌面凭证，请稍后重试。") from None
            await asyncio.sleep(.5)
    identity = await fetch("", label, "desktop_me", session.token)
    verify_identity(identity, email=email, subject=session.subject)
    return session, email


def login_url(flow, challenge):
    return LOGIN_URL + "?" + urlencode({"challenge": challenge, "uuid": flow, "mode": "login"})


async def poll_session(fetch, label, flow, verifier):
    """Return the issued desktop session, or None while the browser has not confirmed."""
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
