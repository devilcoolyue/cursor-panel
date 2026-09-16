"""Generate inspectable local commands; never execute them on the server."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


SCRIPTS = Path(__file__).with_name("scripts")
USER_ID = re.compile(r"user_[A-Za-z0-9_-]+\Z")
SUBJECT = re.compile(r"(?:[A-Za-z0-9_-]+\|)?(user_[A-Za-z0-9_-]+)\Z")
JWT_PART = re.compile(r"[A-Za-z0-9_-]+\Z")


class DesktopSessionError(ValueError):
    pass


class CookieSessionError(DesktopSessionError):
    """Local input validation, separate from upstream desktop credential failures."""
    def __init__(self, message: str, code: str = "invalid_session_cookie"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DesktopSession:
    token: str
    subject: str
    expires_at: int
    refresh_token: str = ""
    token_type: str = "web"


def login_challenge() -> tuple[str, str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return str(uuid.uuid4()), verifier, challenge


def parse_session(cookie: str) -> DesktopSession:
    """Decode claims for format/expiry checks. The API separately verifies with Cursor."""
    value = unquote(cookie.strip())
    if value.startswith("WorkosCursorSessionToken="):
        value = value.partition("=")[2]
    user_id, separator, token = value.partition("::")
    if not separator or not USER_ID.fullmatch(user_id) or len(token) > 12000:
        raise CookieSessionError("此 Cookie 不支持桌面切换，请重新粘贴完整的会话 Cookie。")
    try:
        claims = token_claims(token)
    except DesktopSessionError as error:
        raise CookieSessionError(str(error)) from None
    subject = claims.get("sub")
    match = SUBJECT.fullmatch(subject) if isinstance(subject, str) else None
    if not match or match.group(1) != user_id:
        raise CookieSessionError("Cookie 的账号与 Token 不一致，请重新授权。", "cookie_account_mismatch")
    # The Cookie contains a bare user ID. Keep the full subject for subsequent
    # provider identity checks; different OAuth providers are not interchangeable.
    return DesktopSession(token, subject, int(claims["exp"]), token_type=claims.get("type", ""))


def token_claims(token: str) -> dict:
    if not isinstance(token, str) or len(token) > 12000:
        raise DesktopSessionError("Token 格式无效。")
    parts = token.split(".")
    if len(parts) != 3 or not all(JWT_PART.fullmatch(part) for part in parts):
        raise DesktopSessionError("Cookie 中的 Token 格式无效。")
    try:
        claims = json.loads(base64.b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
        ))
    except (ValueError, UnicodeError, binascii.Error):
        raise DesktopSessionError("无法解析 Cookie 中的 Token。") from None
    if not isinstance(claims, dict):
        raise DesktopSessionError("Token 内容无效。")
    expiry = claims.get("exp")
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not time.time() < expiry <= 253402300799):
        raise DesktopSessionError("Token 已过期或缺少有效的过期时间，请重新授权。")
    return claims


def desktop_session(data: dict, expected_subject: str) -> DesktopSession:
    if not isinstance(data, dict):
        raise DesktopSessionError("Cursor 未返回桌面登录凭证。")
    token = data.get("accessToken")
    refresh = data.get("refreshToken")
    claims = token_claims(token)
    subject = claims.get("sub")
    if claims.get("type") != "session":
        raise DesktopSessionError("Cursor 未返回桌面 session 凭证，不能生成切换命令。")
    if (not isinstance(subject, str)
            or subject.removeprefix("auth0|") != expected_subject.removeprefix("auth0|")):
        raise DesktopSessionError("桌面凭证与所选账号不一致。")
    if not isinstance(refresh, str) or not refresh or len(refresh) > 12000:
        raise DesktopSessionError("Cursor 未返回有效的桌面刷新凭证。")
    return DesktopSession(token, subject, int(claims["exp"]), refresh, "session")


def refreshed_session(data: dict, expected_subject: str) -> DesktopSession:
    if not isinstance(data, dict) or data.get("shouldLogout") is True:
        raise DesktopSessionError("桌面会话已被撤销，请重新授权。")
    return desktop_session({"accessToken": data.get("access_token"),
                            "refreshToken": data.get("refresh_token") or data.get("access_token")},
                           expected_subject)


def build_commands(session: DesktopSession, email: str, *, preview: bool = False, backup_retention: bool = False) -> dict:
    if not preview:
        desktop_session({"accessToken": session.token, "refreshToken": session.refresh_token}, session.subject)
    payload = json.dumps({
        "token": session.token,
        "refreshToken": session.refresh_token,
        "subject": session.subject,
        "email": email,
        "expiresAt": session.expires_at,
        "preview": preview,
    }, ensure_ascii=True, separators=(",", ":"))
    engine = SCRIPTS.joinpath("switch-account.cjs").read_text(encoding="utf-8")
    engine = engine.replace("// __BACKUP_RETENTION__", SCRIPTS.joinpath("prune-backups.cjs").read_text(
        encoding="utf-8") if backup_retention else "")
    engine = engine.replace("__SESSION_JSON__", payload)
    result = {}
    for platform, filename in (("macos", "switch-macos.sh"), ("windows", "switch-windows.ps1")):
        script = SCRIPTS.joinpath(filename).read_text(encoding="utf-8")
        script = script.replace("__EXPIRES_AT__", str(session.expires_at))
        script = script.replace("__ENGINE__", engine)
        if preview:
            script = ("exit 1 # 仅供预览，不能执行切换\n" if platform == "macos"
                      else "throw '仅供预览，不能执行切换'\n") + script
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        if platform == "macos":
            command = f"printf %s '{encoded}' | /usr/bin/openssl base64 -A -d | /bin/bash"
        else:
            # Run inside the user's PowerShell; avoid cmd.exe's 8191-character limit.
            command = ("& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString("
                       f"[Convert]::FromBase64String('{encoded}'))))")
        result[platform] = {"command": command, "script": script}
    return {"commands": result, "expires_at": session.expires_at}
