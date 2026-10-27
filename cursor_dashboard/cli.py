"""命令行入口：把每个账号的额度打印到终端。

    cursor-quota                # 终端进度条
    cursor-quota --json         # 结构化输出，便于入库
    cursor-quota -c other.json  # 按旧版 JSON 的账号逐个在浏览器中确认后查询

默认读取 Web 面板共用的 SQLite 账号库；旧 JSON 格式见 accounts.example.json。
Cursor 已不再接受在浏览器外用 Cookie 换取桌面凭证：-c 只使用文件中的名称，
逐个打开 Cursor 登录确认页，在浏览器中用对应账号确认后查询，凭证不保存。

全部成功退出码 0，任一账号失败为 1，方便挂定时任务。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import sessions
from .client import AuthExpired, DESKTOP_ENDPOINTS, RateLimited, fetch_one
from .config import REQUEST_MIN_INTERVAL
from .desktop import DesktopSessionError, login_challenge
from .store import AccountsError, load_accounts
from .usage import assemble_desktop


def bar(p, width=24):
    filled = int(round(width * min(100.0, max(0.0, p)) / 100))
    return "█" * filled + "░" * (width - filled)


def render(d: dict) -> str:
    q, c, p = d["quota"], d["cycle"], d["plan"]
    lines = [
        "=" * 58,
        f"{d['label']}  <{d['email']}>",
        f"套餐: {p['name']} {p['price'] or ''}   包含额度: ${p['included_usd']}",
        f"刷新: {(c['reset_at'] or '未知')[:19]} UTC" + (f"  (剩 {c['days_left']} 天)" if c["days_left"] is not None else ""),
        "-" * 58,
        f"Cursor Models (Auto/Composer/Grok)  剩余 {q['cursor_models']['remaining_pct']:5.1f}%  {bar(q['cursor_models']['used_pct'])}",
        f"Other Models  (第三方高级模型)      剩余 {q['other_models']['remaining_pct']:5.1f}%  {bar(q['other_models']['used_pct'])}",
        f"综合                                剩余 {q['overall']['remaining_pct']:5.1f}%  {bar(q['overall']['used_pct'])}",
        "-" * 58,
        f"本周期消费 ${d['spend_usd']['total']} "
        f"(含额度 ${d['spend_usd']['from_included']} + 赠送 ${d['spend_usd']['from_bonus']})",
        f"按量付费: {'开启' if d['on_demand']['enabled'] else '关闭'}"
        + (f"  已用 ${d['on_demand']['used_usd']}" if d["on_demand"]["enabled"] else ""),
    ]
    if d.get("grok_weekly"):
        g = d["grok_weekly"]
        lines.append(f"Grok Bot 周额度: 剩余 {g['remaining_pct']:.1f}%  下次刷新 {(g['reset_at'] or '')[:19]} UTC")
    if d.get("notice"):
        lines.append(f"⚠ {d['notice']}")
    return "\n".join(lines)


async def browser_login(label, fetch, *, open_browser=webbrowser.open, timeout=180, interval=2):
    """等待用户在浏览器中确认 Cursor 桌面登录页。"""
    flow, verifier, challenge = login_challenge()
    url = sessions.login_url(flow, challenge)
    print(f"[{label}] 请在浏览器中用该账号确认登录（{timeout // 60} 分钟内有效）：\n{url}", file=sys.stderr, flush=True)
    open_browser(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = await sessions.poll_session(fetch, label, flow, verifier)
        if session:
            return session
        await asyncio.sleep(interval)
    raise DesktopSessionError("等待浏览器确认超时，请重新运行。")


async def query_accounts(accounts, *, temporary=False, login=browser_login):
    async def fetch(cookie, label, name, *args):
        await asyncio.sleep(REQUEST_MIN_INTERVAL)
        return await asyncio.to_thread(fetch_one, cookie, label, name, *args)

    results, failed = [], 0
    for account in accounts:
        label = account.get("label") or "unnamed"
        try:
            if temporary:
                session = await login(label, fetch)
                raw = [await fetch("", label, name, session.token) for name in DESKTOP_ENDPOINTS]
                sessions.verify_identity(raw[0], email=account.get("email"), subject=session.subject)
            else:
                account = await sessions.ensure_account(account, fetch)
                raw = [await sessions.request_account(account, fetch, name) for name in DESKTOP_ENDPOINTS]
                sessions.verify_identity(raw[0], email=account.get("email"), subject=account["auth_subject"])
            results.append(assemble_desktop(label, *raw))
        except (AuthExpired, RateLimited, DesktopSessionError, AccountsError) as exc:
            failed += 1
            results.append({"label": label, "error": str(exc)})
        except Exception:
            failed += 1
            results.append({"label": label, "error": "查询失败，请稍后重试"})
    return results, failed


def main():
    ap = argparse.ArgumentParser(description="查询 Cursor 账号额度")
    ap.add_argument("-c", "--config", help="按旧版 JSON 文件中的账号逐个在浏览器中确认后查询")
    ap.add_argument("--json", action="store_true", help="输出 JSON，方便入库")
    args = ap.parse_args()

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            sys.exit(f"找不到配置文件 {cfg_path}")
        accounts = json.loads(cfg_path.read_text(encoding="utf-8"))
    else:
        try:
            accounts = load_accounts()
        except AccountsError as exc:
            sys.exit(str(exc))
    if not accounts:
        sys.exit("账号库为空，请先在 Web 面板添加账号")

    results, failed = asyncio.run(query_accounts(accounts, temporary=bool(args.config)))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(render(r) if "error" not in r else f"\n[{r['label']}] ✗ {r['error']}")
        print("=" * 58)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
