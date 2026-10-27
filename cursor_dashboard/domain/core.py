from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class CoreError(RuntimeError):
    """Safe, actionable messages suitable for the local maintenance CLI."""


class NotFound(CoreError):
    pass


class Forbidden(CoreError):
    pass


class Unauthenticated(CoreError):
    pass


class Throttled(CoreError):
    pass


class Conflict(CoreError):
    pass


class AuthorizationRejected(Conflict):
    """An authorization outcome the user can act on; `code` selects the public message."""
    def __init__(self, message, code, status=409):
        super().__init__(message)
        self.code, self.status = code, status


class Locked(CoreError):
    pass


class SecretError(CoreError):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: str
    # Only trusted local maintenance may omit a session. HTTP constructs this
    # from a verified random ticket and rechecks it throughout each use case.
    session_id: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class AccountRef:
    workspace_id: str
    account_id: str
    generation: str
    version: int


@dataclass(frozen=True, repr=False)
class Secrets:
    cookie: str = ""
    access_token: str = ""
    refresh_token: str = ""

    def __repr__(self):
        return "Secrets(<redacted>)"


@dataclass(frozen=True)
class AuthorizedAccount:
    ref: AccountRef
    label: str
    email: str | None
    subject: str | None
    expires_at: int
    refreshed_at: int
    invalid: bool
    secrets: Secrets = field(repr=False)


class KeyProvider(Protocol):
    @property
    def active_id(self) -> str: ...
    def get(self, key_id: str) -> bytes: ...


class AccountRepository(Protocol):
    def authorized(self, workspace_id: str, account_id: str) -> AuthorizedAccount: ...
    def claim_lease(self, workspace_id: str, account_id: str, owner: str, ttl: float) -> bool: ...
    def renew_lease(self, workspace_id: str, account_id: str, owner: str, ttl: float) -> bool: ...
    def release_lease(self, workspace_id: str, account_id: str, owner: str) -> None: ...
    def rotate(self, account: AuthorizedAccount, secrets: Secrets, expires_at: int,
               *, lease_owner: str, invalid: bool = False, subject: str | None = None) -> bool: ...
