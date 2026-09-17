"""每个账号"最后一次已知状态"的存放处，取代原来的 TTL 缓存。

和缓存最关键的区别是**失败不覆盖成功**：一次限流只写 error，data 原样留着，
页面继续显示上一份数据并挂一行"数据有点旧"。这是误报的根治办法——过去 403 一来
就把整张卡覆盖成"会话失效"，好数据被擦掉，只能靠重启服务清缓存才能恢复。

内存是主副本，写入时同步落 SQLite（见 store.save_snapshot），只为了重启后页面
立刻有数据、调度器也知道该先刷谁。落盘内容不含 cookie。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

from . import pools
from .store import (
    AccountsError,
    delete_snapshot,
    fingerprint,
    load_snapshots,
    save_snapshot,
)

# ident -> {"fingerprint", "data", "ok_at", "error", "failures", "attempted_at"}
_snapshots: dict[str, dict] = {}
_lock = threading.Lock()

# ident -> asyncio.Lock，防止调度器和手动刷新同时打同一个账号
_inflight: dict[str, asyncio.Lock] = {}

# 认证失效要连续确认这么多次才敢标红。一次就信的代价太大：限流被误判成失效时，
# 用户会去重新粘贴 cookie，而那次粘贴同样会被挡住，看起来就像"新 cookie 也不行"。
EXPIRE_CONFIRMATIONS = 2


def lock_for(ident: str) -> asyncio.Lock:
    return _inflight.setdefault(ident, asyncio.Lock())


def load() -> int:
    """启动时把落盘的快照读回内存。数据库读不出来不该拦住服务启动。"""
    global _snapshots
    try:
        loaded = load_snapshots()
    except AccountsError:
        loaded = {}
    with _lock:
        _snapshots = loaded
    # 重启后立刻把套餐池表建起来，否则用满的账号要等到同套餐里有人刷新成功才显示上限
    pools.reset()
    for ident, snap in loaded.items():
        pools.observe(ident, snap.get("data"))
    return len(loaded)


def _blank(fp: str) -> dict:
    return {"fingerprint": fp, "data": None, "ok_at": 0,
            "error": None, "failures": 0, "attempted_at": 0}


def get(ident: str, cookie: str) -> dict:
    """取快照。cookie 换过就从空白开始——旧账号的数据不能挂在新会话上。"""
    fp = fingerprint(cookie)
    with _lock:
        snap = _snapshots.get(ident)
        if not snap or snap["fingerprint"] != fp:
            return _blank(fp)
        return dict(snap)


def _store(ident: str, snap: dict) -> dict:
    with _lock:
        _snapshots[ident] = snap
    try:
        save_snapshot(ident, snap)
    except AccountsError:
        pass          # 落盘失败不影响内存里的结果，下次写入还会再试
    return snap


def record_success(ident: str, cookie: str, data: dict) -> dict:
    now = int(time.time())
    data = pools.retain_own_limits(data, get(ident, cookie)["data"])
    pools.observe(ident, data)
    return _store(ident, {
        "fingerprint": fingerprint(cookie),
        "data": data,
        "ok_at": now,
        "error": None,
        "failures": 0,
        "attempted_at": now,
    })


def record_failure(ident: str, cookie: str, kind: str, message: str) -> dict:
    """只写失败信息，绝不动 data。

    failures 按"同一种失败连续出现几次"计数，换了错误类型就归零重数——
    先被限流一次再真失效一次，不该被凑成"确认失效"。
    """
    now = int(time.time())
    previous = get(ident, cookie)
    same_kind = (previous["error"] or {}).get("kind") == kind
    return _store(ident, {
        "fingerprint": fingerprint(cookie),
        "data": previous["data"],
        "ok_at": previous["ok_at"],
        "error": {"kind": kind, "message": message, "at": now},
        "failures": previous["failures"] + 1 if same_kind else 1,
        "attempted_at": now,
    })


def drop(ident: str) -> None:
    with _lock:
        _snapshots.pop(ident, None)
    _inflight.pop(ident, None)
    pools.forget(ident)
    try:
        delete_snapshot(ident)
    except AccountsError:
        pass


def attempted_at(ident: str, cookie: str) -> int:
    return get(ident, cookie)["attempted_at"]


# ---------- 给前端的视图 ----------

def _iso(ts: int) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def view(acc: dict, ident: str, snap: dict | None = None) -> dict:
    """把账号信息和快照拼成卡片要的结构。永远不回传 cookie。"""
    snap = snap if snap is not None else get(ident, acc["cookie"])
    # 只用与本账号已知容量相符的同套餐观测补齐；存在歧义的上限留空。
    data = pools.fill(snap["data"])
    error = snap["error"]
    expired = bool(
        error
        and error["kind"] == "expired"
        and (data is None or snap["failures"] >= EXPIRE_CONFIRMATIONS)
    )
    return {
        "id": ident,
        "label": acc.get("label") or "unnamed",
        "email": (data or {}).get("email") or acc.get("email"),
        "department": acc.get("department") or "",
        "auth": {
            "mode": "desktop" if acc.get("refresh_token") else "legacy_cookie",
            "expires_at": _iso(acc.get("token_expires_at", 0)),
            "refreshed_at": _iso(acc.get("auth_refreshed_at", 0)),
            "needs_reauthorization": bool(acc.get("auth_invalid")),
        },
        "ok": bool(data) and not expired,
        "data": data,
        # 数据是什么时候统计出来的——卡片上显示的就是这个，不是"页面打开时间"
        "ok_at": _iso(snap["ok_at"]),
        "age": round(time.time() - snap["ok_at"], 1) if snap["ok_at"] else None,
        "checked_at": _iso(snap["attempted_at"]),
        # 有数据但最近一次刷新失败：卡片正常显示，只挂一条提醒
        "stale": bool(data) and error is not None and not expired,
        "expired": expired,
        "error_kind": error["kind"] if error else None,
        "error": error["message"] if error else None,
        # 从没成功过也没失败过——后台还没轮到它
        "pending": data is None and error is None,
    }
