from __future__ import annotations

import asyncio
from contextlib import suppress
import secrets
import time

from ..client import AuthExpired
from ..desktop import refreshed_session
from ..domain.core import AccountRepository, Conflict, Secrets


class CredentialService:
    def __init__(self, repository: AccountRepository, gateway, config):
        self.repository, self.gateway, self.config = repository, gateway, config

    def usable(self, account, rejected_token):
        lifetime = account.expires_at - account.refreshed_at
        margin = min(self.config.refresh_margin, max(30, lifetime * .2))
        return (not account.invalid and account.secrets.access_token and account.secrets.refresh_token
                and account.expires_at > time.time() + margin
                and account.secrets.access_token != rejected_token)

    async def ensure(self, workspace_id, account_id, *, rejected_token=None):
        repo = self.repository
        owner = secrets.token_hex(16)
        deadline = time.monotonic() + self.config.lease_wait
        while True:
            account = await asyncio.to_thread(repo.authorized, workspace_id, account_id)
            if account.invalid:
                raise AuthExpired("Desktop authorization has been revoked")
            if self.usable(account, rejected_token):
                return account
            if await asyncio.to_thread(repo.claim_lease, workspace_id, account_id, owner, self.config.lease_ttl):
                break
            if time.monotonic() >= deadline:
                raise Conflict("Authorization refresh is still in progress; retry later")
            await asyncio.sleep(.05)

        async def heartbeat():
            while True:
                await asyncio.sleep(self.config.lease_ttl / 3)
                if not await asyncio.to_thread(repo.renew_lease, workspace_id, account_id, owner, self.config.lease_ttl):
                    return

        task = asyncio.create_task(heartbeat())
        try:
            account = await asyncio.to_thread(repo.authorized, workspace_id, account_id)
            if account.invalid:
                raise AuthExpired("Desktop authorization has been revoked")
            if self.usable(account, rejected_token):
                return account
            try:
                if not account.secrets.refresh_token:
                    # Cursor no longer exchanges web cookies outside the browser.
                    raise AuthExpired("Legacy cookie requires browser reauthorization")
                raw = await self.gateway("", account.label, "desktop_refresh", account.secrets.refresh_token)
                if isinstance(raw, dict) and raw.get("shouldLogout") is True:
                    raise AuthExpired("Desktop authorization has been revoked")
                renewed = refreshed_session(raw, account.subject or "")
                payload = Secrets(account.secrets.cookie, renewed.token, renewed.refresh_token)
                saved = await asyncio.to_thread(repo.rotate, account, payload, renewed.expires_at,
                                                lease_owner=owner, subject=renewed.subject)
            except AuthExpired:
                saved = await asyncio.to_thread(repo.rotate, account, account.secrets, account.expires_at,
                                                lease_owner=owner, invalid=True)
                if saved:
                    raise
            current = await asyncio.to_thread(repo.authorized, workspace_id, account_id)
            if current.invalid:
                raise AuthExpired("Desktop authorization has been revoked")
            if not saved and current.ref == account.ref:
                raise Conflict("Refresh lease expired; retry with current authorization")
            return current
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(repo.release_lease, workspace_id, account_id, owner)

    async def request(self, workspace_id, account_id, name, *args):
        account = await self.ensure(workspace_id, account_id)
        try:
            return await self.gateway("", account.label, name, account.secrets.access_token, *args)
        except AuthExpired:
            account = await self.ensure(workspace_id, account_id, rejected_token=account.secrets.access_token)
            return await self.gateway("", account.label, name, account.secrets.access_token, *args)
