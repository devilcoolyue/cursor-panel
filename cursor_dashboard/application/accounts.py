from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import uuid

import requests

from ..client import RateLimited
from ..desktop import DesktopSessionError, login_challenge
from ..domain.core import AuthorizationRejected, Conflict, NotFound, Secrets, Throttled
from ..infrastructure.providers.cursor.authorization import login_url, poll_session, verify_identity
from ..pools import fill_visible
from ..usage import assemble_detail, iso_to_dt
from .queries import QueryFailure, classify, collect_usage
from .security import Limiter

AUTHORIZATION_TTL = 300
AUTHORIZATION_POLL_INTERVAL = 1.5
AUTHORIZATION_RETRY_DELAY = 5
AUTHORIZATION_MAX_FAILURES = 5
MAX_AUTHORIZATIONS = 64
MAX_USER_AUTHORIZATIONS = 3


@dataclass(eq=False)
class BrowserAuthorization:
    """One pending desktop login confirmed in the user's browser; kept only in memory."""
    user_id: str
    workspace_id: str
    account_id: str | None
    label: str | None
    tags: tuple
    flow: str
    verifier: str = field(repr=False)
    expires_at: int = 0
    next_poll: float = 0.0
    failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class AccountService:
    def __init__(self, repository, credentials, gateway, config):
        self.repo, self.credentials, self.gateway, self.config = repository, credentials, gateway, config
        self.limiter = Limiter()
        self._locks = {}
        self._details = OrderedDict()
        self._authorizations = {}

    @asynccontextmanager
    async def _account_lock(self, key):
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)

    def list(self, actor, workspace_id):
        rows = self.repo.list_views(actor, workspace_id)
        visible_data = [row["data"] for row in rows if row["data"]]
        for row in rows:
            row["data"] = fill_visible(row["data"], visible_data)
            row["expired"] = bool(row["error_kind"] == "expired" and (row["data"] is None or row["failures"] >= 2))
            row["stale"] = bool(row["data"] is not None and row["error_kind"] and not row["expired"])
            row["pending"] = row["data"] is None and row["error_kind"] is None
        return rows

    def search(self, actor, workspace_id, *, query="", tag=None, offset=0, limit=50):
        if not 1 <= limit <= 200 or offset < 0 or len(query) > 256:
            raise Conflict("Invalid account pagination or search")
        rows = self.list(actor, workspace_id)
        query = query.strip().casefold()
        rows = [r for r in rows if (not query or query in (r["label"] + " " + (r["email"] or "")).casefold())
                and (tag is None or tag in r["tags"])]
        tags = {}
        for row in rows:
            for name in row["tags"]:
                tags[name] = tags.get(name, 0) + 1
        return {"items": rows[offset:offset + limit], "total": len(rows), "tags": tags,
                "stats": {"accounts": len(rows), "invalid": sum(bool(r["auth_invalid"]) for r in rows),
                          "with_snapshot": sum(r["data"] is not None for r in rows)}}

    def get(self, actor, workspace_id, account_id):
        return next((row for row in self.list(actor, workspace_id) if row["id"] == account_id), None) or self._missing()

    @staticmethod
    def _missing():
        raise NotFound("Workspace or account is unavailable")

    async def begin_authorization(self, actor, workspace_id, *, label=None, tags=(), account_id=None):
        self.repo.check_access(actor, workspace_id, account_id, "manage")
        if account_id:
            self.repo.authorized(workspace_id, account_id)
        self.limiter.manual(actor, workspace_id, account_id)
        now = int(time.time())
        for key in [key for key, item in self._authorizations.items() if item.expires_at + AUTHORIZATION_TTL <= now]:
            del self._authorizations[key]
        # A new attempt supersedes the user's oldest ones, so one user cannot fill the shared capacity.
        own = [key for key, item in self._authorizations.items() if item.user_id == actor.user_id]
        for key in own[:max(0, len(own) - MAX_USER_AUTHORIZATIONS + 1)]:
            del self._authorizations[key]
        if len(self._authorizations) >= MAX_AUTHORIZATIONS:
            raise Throttled("Too many pending authorizations; retry later")
        flow, verifier, challenge = login_challenge()
        key = str(uuid.uuid4())
        self._authorizations[key] = BrowserAuthorization(actor.user_id, workspace_id, account_id, label, tuple(tags),
                                                         flow, verifier, now + AUTHORIZATION_TTL)
        return {"id": key, "login_url": login_url(flow, challenge), "expires_at": now + AUTHORIZATION_TTL}

    def _own_authorization(self, actor, workspace_id, authorization_id):
        item = self._authorizations.get(authorization_id)
        if item is None or item.user_id != actor.user_id or item.workspace_id != workspace_id:
            return None
        return item

    async def poll_authorization(self, actor, workspace_id, authorization_id):
        item = self._own_authorization(actor, workspace_id, authorization_id) or self._missing()
        self.repo.check_access(actor, workspace_id, item.account_id, "manage")
        async with item.lock:
            if self._authorizations.get(authorization_id) is not item:
                self._missing()
            pending = {"status": "pending", "expires_at": item.expires_at, "account": None}
            if time.time() >= item.expires_at:
                self._authorizations.pop(authorization_id, None)
                raise AuthorizationRejected("Browser authorization expired; start again", "authorization_expired")
            if time.monotonic() < item.next_poll:
                return pending
            item.next_poll = time.monotonic() + AUTHORIZATION_POLL_INTERVAL
            try:
                session = await poll_session(self.gateway, item.label or "Account", item.flow, item.verifier)
            except (RateLimited, requests.RequestException):
                # The browser confirmation can still arrive after a transient upstream failure.
                item.failures += 1
                if item.failures < AUTHORIZATION_MAX_FAILURES:
                    item.next_poll = time.monotonic() + AUTHORIZATION_RETRY_DELAY
                    return pending
                self._authorizations.pop(authorization_id, None)
                raise
            except Exception:
                self._authorizations.pop(authorization_id, None)
                raise
            if session is None:
                item.failures = 0
                return pending
            # A poll result is single-use, and a cancellation during the poll still wins.
            if self._authorizations.pop(authorization_id, None) is not item:
                self._missing()
            account = await self._complete_authorization(actor, item, session)
            return {"status": "completed", "expires_at": item.expires_at, "account": account}

    async def _complete_authorization(self, actor, item, session):
        workspace_id = item.workspace_id
        self.repo.check_access(actor, workspace_id, item.account_id, "manage")
        expected = self.repo.authorized(workspace_id, item.account_id) if item.account_id else None
        mismatch = AuthorizationRejected("The browser confirmed another Cursor account",
                                         "authorization_account_mismatch", 422)
        if expected and expected.subject and expected.subject != session.subject.removeprefix("auth0|"):
            raise mismatch
        label = item.label or (expected.label if expected else None)
        identity = await self.gateway("", label or "Account", "desktop_me", session.token)
        try:
            email = verify_identity(identity, email=expected.email if expected else None, subject=session.subject)
        except DesktopSessionError:
            raise mismatch from None
        data = await collect_usage(label or email, lambda name: self.gateway("", label or email, name, session.token),
                                   email=email, subject=session.subject, verify=verify_identity)
        saved = self.repo.put_authorization(actor, workspace_id, email=email, subject=session.subject,
                    label=item.label, secrets=Secrets("", session.token, session.refresh_token),
                    expires_at=session.expires_at, expected=expected.ref if expected else None, data=data,
                    tags=item.tags)
        return self.get(actor, workspace_id, saved.ref.account_id)

    def cancel_authorization(self, actor, workspace_id, authorization_id):
        if self._own_authorization(actor, workspace_id, authorization_id):
            del self._authorizations[authorization_id]

    async def refresh(self, actor, workspace_id, account_id):
        self.repo.check_access(actor, workspace_id, account_id, "use")
        self.limiter.manual(actor, workspace_id, account_id)
        async with self._account_lock((workspace_id, account_id)):
            self.repo.check_access(actor, workspace_id, account_id, "use")
            account = self.repo.authorized(workspace_id, account_id)
            try:
                account = await self.credentials.ensure(workspace_id, account_id)
                data = await collect_usage(account.label,
                    lambda name: self.credentials.request(workspace_id, account_id, name),
                    email=account.email, subject=account.subject, verify=verify_identity)
            except Exception as error:
                errors = error.errors if isinstance(error, QueryFailure) else [error]
                self.repo.check_access(actor, workspace_id, account_id, "use")
                saved = self.repo.record_snapshot(account.ref, error=classify(errors), actor=actor)
            else:
                self.repo.check_access(actor, workspace_id, account_id, "use")
                saved = self.repo.record_snapshot(account.ref, data=data, actor=actor)
            if not saved:
                raise Conflict("Authorization changed while refreshing; old result discarded")
            return self.get(actor, workspace_id, account_id)

    async def detail(self, actor, workspace_id, account_id):
        row = self.get(actor, workspace_id, account_id)
        self.limiter.manual(actor, workspace_id, account_id)
        data = row["data"] or {}
        start_text = (data.get("cycle") or {}).get("start")
        start = iso_to_dt(start_text)
        if start is None:
            raise Conflict("Account billing period is unavailable; refresh the account first")
        key = (workspace_id, account_id, row["authorization_generation"], start_text)
        cached = self._details.get(key)
        if cached and time.monotonic() - cached[0] < self.config.detail_ttl:
            self._details.move_to_end(key)
            return deepcopy(cached[1])
        now = datetime.now(timezone.utc)
        raw = await self.credentials.request(workspace_id, account_id, "desktop_aggregated",
                                             int(start.timestamp() * 1000), int(now.timestamp() * 1000))
        current = self.get(actor, workspace_id, account_id)
        current_start = ((current["data"] or {}).get("cycle") or {}).get("start")
        if current["authorization_generation"] != key[2] or current_start != start_text:
            raise Conflict("Authorization or billing period changed; old detail discarded")
        result = {**assemble_detail(raw), "id": account_id, "workspace_id": workspace_id,
                  "cycle_start": start_text, "fetched_at": now.isoformat()}
        self._details[key] = (time.monotonic(), deepcopy(result))
        self._details.move_to_end(key)
        while len(self._details) > 128:
            self._details.popitem(last=False)
        return result

    def edit(self, actor, workspace_id, account_id, **changes):
        self.repo.edit(actor, workspace_id, account_id, **changes)
        return self.get(actor, workspace_id, account_id)

    def delete(self, actor, workspace_id, account_id):
        self.repo.delete(actor, workspace_id, account_id)
        for key in list(self._details):
            if key[:2] == (workspace_id, account_id):
                del self._details[key]
