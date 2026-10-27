"""Query orchestration shared by the legacy HTTP adapter and V2 services."""
from __future__ import annotations

import asyncio
import requests

from ..client import DESKTOP_ENDPOINTS, AuthExpired, RateLimited
from ..usage import assemble_desktop


class QueryFailure(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Provider query failed")


def classify(errors):
    if any(isinstance(e, RateLimited) for e in errors):
        return "rate_limited", "Cursor 暂时限制了请求，稍后会自动重试"
    if any(isinstance(e, AuthExpired) for e in errors):
        return "expired", "桌面授权已失效，请重新授权"
    if any(isinstance(e, requests.Timeout) for e in errors):
        return "network", "连接 Cursor 超时，稍后会自动重试"
    if any(isinstance(e, requests.ConnectionError) for e in errors):
        return "network", "暂时无法连接 Cursor，稍后会自动重试"
    return "error", f"{type(errors[0]).__name__}: 暂时无法更新账号，请稍后重试"


async def collect_usage(label, request, *, email, subject, verify, assemble=assemble_desktop):
    raw = await asyncio.gather(*(request(name) for name in DESKTOP_ENDPOINTS), return_exceptions=True)
    errors = [item for item in raw if isinstance(item, BaseException)]
    if errors:
        raise QueryFailure(errors)
    verify(raw[0], email=email, subject=subject)
    return assemble(label, *raw)
