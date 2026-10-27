"""Run the real administrator UI against isolated synthetic accounts.

Run: uv run python dev/preview-admin.py --port 8792
Administrator password: preview-admin-password

Browser authorization is synthetic: each login is pending on its first poll and
confirmed on the next, as the subject named in the account label (for example
``user_added``), otherwise ``user_preview_browser``. No request reaches Cursor.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


PASSWORD = "preview-admin-password"


def token(subject, expiry, kind):
    claims = {"sub": subject, "exp": expiry, "type": "session", "kind": kind,
              "preview": True}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + encoded + ".preview"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    if "cursor_dashboard.config" in sys.modules:
        raise RuntimeError("Run this preview as a standalone process.")

    with tempfile.TemporaryDirectory(prefix="cursor-admin-preview-") as directory:
        os.environ.update({
            "DATABASE_PATH": str(Path(directory) / "preview.db"),
            "ACCOUNTS_PATH": str(Path(directory) / "unused-legacy.json"),
            "REFRESH_ENABLED": "0",
            "PANEL_TOKEN": "",
            "ADMIN_PASSWORD": PASSWORD,
        })
        # Configuration must be isolated before importing any application module.
        import uvicorn
        from cursor_dashboard import admin, desktop, server, snapshot, store
        from preview import NAMES, make_account

        now = int(time.time())
        departments = ["Platform", "Product", "Design", "Operations", ""]
        for index in range(27):
            label = NAMES[index] if index < len(NAMES) else NAMES[index % len(NAMES)] + " " + str(index + 1)
            sample = make_account(index, label, departments[index % len(departments)])
            subject = "auth0|user_preview_" + str(index + 1)
            expiry = now + (30 + index) * 86400
            if index == 1:
                expiry = now + 3600
            elif index == 2:
                expiry = now - 3600
            session = desktop.DesktopSession(
                token(subject, expiry, "access"), subject, expiry,
                "opaque-preview-refresh" if index == 5 else token(subject, expiry + 86400, "refresh"),
                "session",
            ) if index != 4 else None
            account = store.upsert_account("preview-cookie-" + str(index + 1), sample["email"], label,
                                           sample["department"], session=session)
            with closing(store._connect()) as conn, conn:
                conn.execute("UPDATE accounts SET auth_refreshed_at = ?, auth_invalid = ? WHERE id = ?",
                             (now - 3 * 86400 if session else 0, int(index == 3), account["db_id"]))
            snapshot.record_success(store.account_id(account), account["cookie"], sample["data"])

        admin.initialize_admin(PASSWORD)

        import requests
        confirmed_flows = set()
        browser_subject = re.compile(r"(?:[a-z0-9-]+\|)?user_[A-Za-z0-9_]+")
        cycle_start = now - 10 * 86400
        browser_usage = {
            "desktop_plan": {"planInfo": {"planName": "Pro", "includedAmountCents": 2000}},
            "desktop_profile": {"membershipType": "pro"},
            "desktop_period": {"billingCycleStart": cycle_start * 1000, "billingCycleEnd": (cycle_start + 30 * 86400) * 1000,
                               "planUsage": {"autoPercentUsed": 28, "apiPercentUsed": 36, "totalPercentUsed": 32,
                                             "totalSpend": 1600, "includedSpend": 1600}},
            "desktop_grok": {"includedLimitZero": True},
            "desktop_limit": {"noUsageBasedAllowed": True},
        }

        async def synthetic_fetch(_cookie, label, name, *values):
            if name == "desktop_poll":
                if values[0] not in confirmed_flows:
                    confirmed_flows.add(values[0])
                    response = requests.Response()
                    response.status_code = 404
                    raise requests.HTTPError("Browser login is not confirmed yet", response=response)
                match = browser_subject.search(label or "")
                subject = match.group(0) if match else "user_preview_browser"
                subject = subject if "|" in subject else "auth0|" + subject
                return {"accessToken": token(subject, now + 30 * 86400, "access"),
                        "refreshToken": token(subject, now + 60 * 86400, "refresh")}
            if name == "desktop_me" and values:
                account = next((item for item in store.load_accounts()
                                if item["access_token"] == values[0]), None)
                if account:
                    return {"email": account["email"], "authId": account["auth_subject"]}
            try:
                claims = desktop.token_claims(values[0]) if values else {}
            except desktop.DesktopSessionError:
                claims = {}
            if claims.get("preview") and name == "desktop_me":
                user = claims["sub"].split("|", 1)[-1]
                return {"email": user.removeprefix("user_") + "@example.test", "authId": claims["sub"]}
            if claims.get("preview") and name in browser_usage:
                return browser_usage[name]
            raise RuntimeError("Preview outbound disabled")

        def preview_commands(session, email):
            return desktop.build_commands(session, email, preview=True)

        server.fetch_cursor = synthetic_fetch
        server.build_commands = preview_commands
        print(f"Synthetic administrator preview: http://127.0.0.1:{args.port}/admin", flush=True)
        print(f"Preview password: {PASSWORD}", flush=True)
        uvicorn.run(server.app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
