"""Compare an independent desktop session before and after manual web sign-out.

prepare: mint test sessions using the account store, then verify refresh + usage.
check: reuse only saved desktop credentials; never exchange the web cookie again.
Credentials stay in a private temporary directory. No Cursor files are modified.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from cursor_dashboard.client import BASE, COOKIE_NAME, CSRF_HEADER, DESKTOP_BASE, UA
from cursor_dashboard.desktop import desktop_session, login_challenge, parse_session
from cursor_dashboard.store import load_accounts


AUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
RPC = DESKTOP_BASE + "/aiserver.v1.DashboardService/"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request(method, url, *, cookie=None, token=None, body=None, params=None):
    # Every request has a fresh cookie jar, so desktop probes cannot inherit web auth.
    with requests.Session() as client:
        client.headers.update({"User-Agent": UA, "Accept": "application/json"})
        if cookie:
            client.cookies.set(COOKIE_NAME, cookie, domain="cursor.com", path="/")
            client.headers.update({"Origin": BASE, "Referer": BASE + "/loginDeepControl"})
        if url.startswith(DESKTOP_BASE):
            client.headers.update({"x-cursor-client-type": "ide", "Connect-Protocol-Version": "1"})
        if token:
            client.headers["Authorization"] = "Bearer " + token
        try:
            if cookie and method != "GET" and url.startswith(BASE):
                # cursor.com POST requires the double-submit CSRF token from this same jar.
                csrf = client.get(BASE + "/api/csrf-token", allow_redirects=False, timeout=15)
                client.headers[CSRF_HEADER] = csrf.json().get("token", "") if csrf.ok else ""
            response = client.request(method, url, json=body, params=params,
                                      allow_redirects=False, timeout=15)
        except requests.RequestException as exc:
            return {"http_status": None, "error": type(exc).__name__}, None
        finally:
            time.sleep(.65)
        try:
            data = response.json()
        except ValueError:
            data = None
        return {"http_status": response.status_code}, data


def identity(cookie=None, token=None, email=""):
    result, data = request("GET" if cookie else "POST", BASE + "/api/auth/me" if cookie else RPC + "GetMe",
                           cookie=cookie, token=token, body=None if cookie else {})
    returned_email = data.get("email") if isinstance(data, dict) else None
    if result["http_status"] == 200 and isinstance(returned_email, str):
        result["identity_matches"] = returned_email.casefold() == email.casefold()
    elif result["http_status"] in (204, 401) or result["http_status"] in (302, 303, 307, 308):
        result["identity_matches"] = False
    else:
        result["identity_matches"] = None
    return result


def save(path, state):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def credentials(session):
    return {"access_token": session.token, "refresh_token": session.refresh_token,
            "subject": session.subject, "expires_at": session.expires_at}


def refresh_and_probe(entry, persist):
    report = {"email": entry["email"], "role": entry["role"], "time": now()}
    report["web_cookie"] = identity(cookie=entry["cookie"], email=entry["email"])
    current = entry["desktop"]
    report["existing_at"] = identity(token=current["access_token"], email=entry["email"])
    result, data = request("POST", DESKTOP_BASE + "/oauth/token", body={
        "grant_type": "refresh_token", "client_id": AUTH_CLIENT_ID,
        "refresh_token": current["refresh_token"],
    })
    report["refresh"] = result
    result["should_logout"] = data.get("shouldLogout") if isinstance(data, dict) else None
    result["valid_credentials_returned"] = False
    if result["http_status"] != 200 or not isinstance(data, dict) or data.get("shouldLogout") is True:
        return report
    try:
        # Current Cursor uses access_token for both fields when no distinct RT is returned.
        refreshed = desktop_session({"accessToken": data.get("access_token"),
                                     "refreshToken": data.get("refresh_token") or data.get("access_token")},
                                    current["subject"])
    except ValueError:
        result["invalid_token_response"] = True
        return report
    result["valid_credentials_returned"] = True
    result["access_token_changed"] = refreshed.token != current["access_token"]
    result["expiry_advanced"] = refreshed.expires_at > current["expires_at"]
    entry["desktop"] = credentials(refreshed)
    persist()  # Keep rotation results even if a subsequent quota request fails.
    report["refreshed_at"] = identity(token=refreshed.token, email=entry["email"])
    usage, payload = request("POST", RPC + "GetCurrentPeriodUsage", token=refreshed.token, body={})
    usage["usage_returned"] = (usage["http_status"] == 200 and isinstance(payload, dict)
                                and "planUsage" in payload)
    report["quota"] = usage
    return report


def prepare(args):
    accounts = {account.get("email"): account for account in load_accounts()}
    if args.experiment == args.control:
        raise ValueError("Experiment and control must be different accounts.")
    selected = [("experiment", args.experiment), ("control", args.control)]
    if any(email not in accounts for _, email in selected):
        raise ValueError("An account is missing from the local store.")
    path = Path(tempfile.mkdtemp(prefix="cursor-session-lifecycle-")) / "state.json"
    state = {"created_at": now(), "accounts": [], "reports": []}
    persist = lambda: save(path, state)
    persist()
    print(json.dumps({"private_state": str(path)}, ensure_ascii=False), flush=True)
    for role, email in selected:
        cookie = accounts[email]["cookie"]
        source = parse_session(cookie)
        web = identity(cookie=cookie, email=email)
        if web.get("identity_matches") is not True:
            raise ValueError(f"Web identity could not be verified for {email}: {web}")
        flow, verifier, challenge = login_challenge()
        callback, _ = request("POST", BASE + "/api/auth/loginDeepCallbackControl", cookie=cookie,
                              body={"uuid": flow, "challenge": challenge})
        if callback["http_status"] not in (200, 204):
            raise ValueError(f"Desktop callback failed for {email}: {callback}")
        for _ in range(5):
            result, data = request("GET", DESKTOP_BASE + "/auth/poll", params={"uuid": flow, "verifier": verifier})
            if result["http_status"] != 404:
                break
        if result["http_status"] != 200:
            raise ValueError(f"Desktop session could not be obtained for {email}: {result}")
        session = desktop_session(data, source.subject)
        entry = {"role": role, "email": email, "cookie": cookie, "desktop": credentials(session)}
        state["accounts"].append(entry)
        persist()
        report = refresh_and_probe(entry, persist)
        report["phase"] = "before_signout"
        state["reports"].append(report)
        persist()
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if (report["web_cookie"].get("identity_matches") is not True
                or report.get("refreshed_at", {}).get("identity_matches") is not True
                or not report.get("quota", {}).get("usage_returned")):
            raise ValueError("Baseline incomplete. Do not sign out yet.")
    state["baseline_ready"] = True
    persist()
    print(json.dumps({"baseline_ready": True, "experiment": args.experiment, "control": args.control,
                      "check_command": f"uv run python dev/verify-session-lifecycle.py check --state {path}"}), flush=True)


def check(args):
    path = Path(args.state).resolve()
    state = json.loads(path.read_text(encoding="utf-8"))
    if not state.get("baseline_ready"):
        raise ValueError("Baseline is incomplete.")
    if path.stat().st_mode & 0o077:
        raise ValueError("Credential file must have permissions 0600.")
    persist = lambda: save(path, state)
    for entry in state["accounts"]:
        report = refresh_and_probe(entry, persist)
        report["phase"] = "after_manual_signout"
        state["reports"].append(report)
        persist()
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    before = commands.add_parser("prepare")
    before.add_argument("--experiment", required=True)
    before.add_argument("--control", required=True)
    after = commands.add_parser("check")
    after.add_argument("--state", required=True)
    args = parser.parse_args()
    try:
        prepare(args) if args.phase == "prepare" else check(args)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
