"""用本账期历史及容量相符的同套餐观测补齐上限，不硬编码套餐额度。

同名套餐可能同时存在不同容量；已知上限是筛选条件，未知容量有歧义时留空。
跨账号补齐仍仅为估算，不能视为远端保证的额度。
"""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime
from math import isfinite
from statistics import median

from .domain.core import Conflict

# Legacy only. V2 supplies the authorized query's observations directly.
_observed: dict[str, dict[str, dict]] = {}
_lock = threading.RLock()
_SLOTS = ("cursor_models", "other_models", "overall")
# A bounded heuristic for exhausted accounts, not a provider guarantee about overshoot.
EXHAUSTED_SPEND_TOLERANCE = .01


def _plan_key(plan: dict | None) -> str:
    plan = plan or {}
    name = (plan.get("name") or plan.get("membership_type") or "").strip()
    return name.lower()


def observe(ident: str, data: dict | None) -> None:
    """只保留账号当前观测；换套餐、换账期或无解时也清除旧观测。"""
    key = _plan_key((data or {}).get("plan"))
    with _lock:
        forget(ident)
        if key and any(value is not None for value in _own_limits(data)):
            _observed.setdefault(key, {})[ident] = deepcopy({
                field: data.get(field) for field in ("plan", "cycle", "quota")
            })


def forget(ident: str) -> None:
    with _lock:
        for key, rows in list(_observed.items()):
            rows.pop(ident, None)
            if not rows:
                del _observed[key]


def resolve(plan: dict | None) -> tuple[float | None, float | None, float | None]:
    """仅返回此套餐中无容量歧义的上限；实际展示还须匹配本账号。"""
    key = _plan_key(plan)
    with _lock:
        seen = list(_observed.get(key, {}).values())
    return _peer_limits({"plan": plan}, seen)[0]


def fill(data: dict | None) -> dict | None:
    key = _plan_key((data or {}).get("plan"))
    with _lock:
        seen = list(_observed.get(key, {}).values())
    return fill_visible(data, seen)


def fill_visible(data: dict | None, visible: list[dict]) -> dict | None:
    """V2 derives observations only from this authorized query, with no global pool state."""
    limits, source = _peer_limits(data, visible)
    return _fill(data, limits, source=source)


def _close(left, right):
    # Allow cent rounding and small estimation noise, not different quota tiers.
    return abs(left - right) <= max(.02, max(left, right) * .001)


def _own_limits(data):
    return tuple(_own_limit(((data or {}).get("quota") or {}).get(slot) or {}) for slot in _SLOTS)


def _compatible(data, limits):
    known = tuple(_valid_limit((((data or {}).get("quota") or {}).get(slot) or {}).get("limit_usd"))
                  for slot in _SLOTS)
    return all(old is None or new is None or _close(old, new) for old, new in zip(known, limits))


def _coherent(limits):
    return any(value is None for value in limits) or _close(limits[0] + limits[1], limits[2])


def _same_plan(data, other):
    plan, peer = (data or {}).get("plan") or {}, (other or {}).get("plan") or {}
    return bool(_plan_key(plan)) and _plan_key(plan) == _plan_key(peer) and all(
        plan.get(key) == peer.get(key) for key in ("included_usd", "price", "membership_type", "unlimited")
    )


def _overlapping_cycles(data, other):
    cycle, peer = (data or {}).get("cycle") or {}, (other or {}).get("cycle") or {}
    if not cycle:
        return True
    start, peer_start = _cycle_date(cycle.get("start")), _cycle_date(peer.get("start"))
    if start is None or peer_start is None:
        return False
    end, peer_end = _cycle_date(cycle.get("reset_at")), _cycle_date(peer.get("reset_at"))
    if end is None or peer_end is None:
        return start == peer_start
    return start < end and peer_start < peer_end and max(start, peer_start) < min(end, peer_end)


def _estimate(values, *, unanimous=False):
    if not values:
        return None
    middle = median(values)
    support = [value for value in values if _close(value, middle)]
    if len(support) == len(values) or (not unanimous and len(support) > len(values) / 2):
        return round(median(support), 2)
    return None


def _exhausted_candidates(data, observations):
    """Use a unique observed capacity close to final spend only as an explicit estimate.

    Capped percentages are lower bounds on usage. Never divide by them or turn final
    spend into a new capacity, and never persist this peer-based estimate as history.
    """
    quota = (data or {}).get("quota") or {}
    if any(_valid_limit((quota.get(key) or {}).get("used_pct")) != 100 for key in _SLOTS):
        return None
    on_demand = (data or {}).get("on_demand") or {}
    if on_demand.get("enabled") is not False or on_demand.get("used_usd") != 0:
        return None
    spend = _valid_limit(((data or {}).get("spend_usd") or {}).get("total"))
    if spend is None or _own_limits(data)[2] is not None:
        return None
    near = [limits for limits in observations if all(value is not None for value in limits)
            and -.02 <= spend - limits[2] <= max(.02, limits[2] * EXHAUSTED_SPEND_TOLERANCE)]
    if not near or _estimate([limits[2] for limits in near], unanimous=True) is None:
        return None
    return near


def _peer_limits(data, visible):
    key = _plan_key((data or {}).get("plan"))
    observations = []
    known_total = _own_limits(data)[2]
    for item in visible:
        if not key or not _same_plan(data, item) or not _overlapping_cycles(data, item):
            continue
        limits = _own_limits(item)
        if not _coherent(limits) or not _compatible(data, limits):
            continue
        if known_total is not None and limits[2] is None:
            # An unanchored model limit cannot establish a matching capacity.
            continue
        observations.append(limits)
    exhausted = _exhausted_candidates(data, observations)
    source = 'exhaustion' if exhausted else 'plan'
    values = [[] for _ in _SLOTS]
    for limits in exhausted or observations:
        for column, value in zip(values, limits):
            if value is not None:
                column.append(value)
    # Without a known total, a majority of one capacity does not identify this account's
    # capacity. Only slots shared by every observed capacity remain usable.
    mixed_totals = bool(values[2]) and _estimate(values[2], unanimous=True) is None
    return tuple(_estimate(column, unanimous=mixed_totals) for column in values), source


def _valid_limit(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 else None


def _own_limit(slot):
    """A peer-derived estimate must never become a new observation or persistent own history."""
    value = slot.get("limit_usd")
    if slot.get("limit_source") in {"plan", "reference", "exhaustion"} or (slot.get("limit_inferred") and slot.get("limit_source") != "history"):
        return None
    return _valid_limit(value)


def _cycle_date(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date if date.tzinfo is not None else None
    except (TypeError, AttributeError, ValueError):
        return None


def retain_own_limits(data: dict | None, previous: dict | None) -> dict | None:
    """Keep solved limits and explicit references within the same account, plan and cycle.

    Called before saving a successful snapshot (inside the V2 write transaction). No peer
    observations are persisted, so grant changes or source deletion affect estimates immediately.
    """
    if not data or not previous:
        return data
    plan, old_plan = data.get("plan") or {}, previous.get("plan") or {}
    if not _plan_key(plan) or _plan_key(plan) != _plan_key(old_plan):
        return data
    if any(plan.get(key) != old_plan.get(key) for key in ("included_usd", "price", "membership_type", "unlimited")):
        return data
    cycle, old_cycle = data.get("cycle") or {}, previous.get("cycle") or {}
    start, old_start = _cycle_date(cycle.get("start")), _cycle_date(old_cycle.get("start"))
    if start is None or start != old_start:
        return data
    # A known end changing or disappearing can indicate a changed billing period.
    if cycle.get("reset_at") or old_cycle.get("reset_at"):
        end, old_end = _cycle_date(cycle.get("reset_at")), _cycle_date(old_cycle.get("reset_at"))
        if end is None or end != old_end or end <= start:
            return data
    limits = tuple(_own_limit((previous.get("quota") or {}).get(slot) or {}) for slot in _SLOTS)
    result = _fill(data, limits, source="history") if _compatible(data, limits) else data
    references = tuple(_valid_limit(slot.get("limit_usd")) if slot.get("limit_source") == "reference" else None
                       for slot in ((previous.get("quota") or {}).get(key) or {} for key in _SLOTS))
    return _fill(result, references, source="reference") if _compatible(result, references) else result


def set_reference_limits(data: dict | None, reference: dict) -> dict:
    """An explicit account-scoped fallback, bound to the displayed billing period.

    References never contribute to peer observations or replace solved limits.
    Empty values remove a previous reference without changing successful query times.
    """
    start = _cycle_date(((data or {}).get("cycle") or {}).get("start"))
    if not data or start is None or start != _cycle_date(reference.get("cycle_start")):
        raise Conflict("Billing period changed; reload the account before saving quota references")
    if not _plan_key(data.get("plan")):
        raise Conflict("Account plan is unavailable")
    if set(reference) - {*_SLOTS, "cycle_start"}:
        raise Conflict("Unknown quota reference")
    limits = tuple(reference.get(key) for key in _SLOTS)
    if any(value is not None and (_valid_limit(value) is None or value > 1e9) for value in limits):
        raise Conflict("Quota references must be positive finite amounts")
    if all(value is not None for value in limits) and abs(limits[0] + limits[1] - limits[2]) > .01:
        raise Conflict("Overall quota reference must equal the sum of both model quotas")
    quota = {key: dict(slot) for key, slot in (data.get("quota") or {}).items()}
    for slot in quota.values():
        if slot.get("limit_source") == "reference":
            slot.update(limit_usd=None, used_usd=None, remaining_usd=None)
            slot.pop("limit_inferred", None)
            slot.pop("limit_source", None)
    return _fill({**data, "quota": quota}, limits, source="reference")


def _fill(data: dict | None, limits, *, source="plan") -> dict | None:
    """给触顶而解不出上限的档位补上同套餐的池子，并标 `limit_inferred`。

    只补 `None` 的档位：账号自己解出来的数永远优先于从别人那儿抄来的。
    """
    quota = (data or {}).get("quota") or {}
    if not quota or all(
        (quota.get(key) or {}).get("limit_usd") is not None
        for key in ("cursor_models", "other_models", "overall")
    ):
        return data

    if all(v is None for v in limits):
        return data

    combined = tuple((quota.get(key) or {}).get("limit_usd") or limit
                     for key, limit in zip(_SLOTS, limits))
    if not _coherent(combined):
        return data

    patched = dict(quota)
    for key, limit in zip(("cursor_models", "other_models", "overall"), limits):
        slot = quota.get(key) or {}
        if slot.get("limit_usd") is not None or limit is None:
            continue
        used_pct = slot.get("used_pct") or 0
        patched[key] = {
            **slot,
            "limit_usd": limit,
            "used_usd": round(limit * used_pct / 100, 2),
            "remaining_usd": round(limit - limit * used_pct / 100, 2),
            # 前端不区分显示，但排查时要能一眼看出这个数不是本账号自己算出来的
            "limit_inferred": True,
            "limit_source": source,
        }
    return {**data, "quota": patched}


def snapshot_state() -> dict[str, int]:
    """/api/status 用：每个套餐现在有几个账号支撑着这张表。"""
    with _lock:
        return {plan: len(rows) for plan, rows in _observed.items()}


def reset() -> None:
    with _lock:
        _observed.clear()
