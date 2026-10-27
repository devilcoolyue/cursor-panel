from __future__ import annotations

import base64
from copy import deepcopy
import json
import time

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.sqlite import insert

from ...domain.core import (AccountRef, AuthorizationRejected, AuthorizedAccount, Conflict,
                            NotFound, SecretError)
from ...pools import retain_own_limits, set_reference_limits
from .policy import audit, authorize, capabilities, membership
from .models import (Account, AccountTag, Credential, Grant, Lease, LegacyImport,
                     Membership, Metadata, Snapshot, Tag, User, Workspace, new_id)


def normalize_email(value):
    return (value or "").strip().casefold() or None


def normalize_subject(value):
    return (value or "").removeprefix("auth0|") or None


class Repository:
    """Trusted persistence adapter; application operations must supply a checked Actor."""
    def __init__(self, database, cipher):
        self.db = database
        self.cipher = cipher

    def check_key(self, *, initialize=False):
        with self.db.transaction(write=initialize) as session:
            record = session.get(Metadata, "key_check")
            if record is None:
                if not initialize:
                    raise SecretError("Database key check is missing; restore a complete backup")
                key_id, data = self.cipher.encrypt(b"cursor-core-v1", ["instance-key-check"])
                session.add(Metadata(name="key_check", value=json.dumps({"key_id": key_id,
                            "payload": base64.b64encode(data).decode("ascii")})))
            else:
                try:
                    data = json.loads(record.value)
                    value = self.cipher.decrypt(data["key_id"], base64.b64decode(data["payload"], validate=True),
                                                ["instance-key-check"])
                    if value != b"cursor-core-v1":
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise SecretError("Database key check is invalid; restore a complete backup") from None

    def create_workspace(self, login, name, kind="personal"):
        login = normalize_email(login)
        if not login or not name.strip() or kind not in {"personal", "team"}:
            raise Conflict("Owner login, workspace name and valid kind are required")
        with self.db.transaction(write=True) as session:
            user = session.scalar(select(User).where(User.login == login))
            if user is None:
                user = User(login=login)
                session.add(user)
                session.flush()
            workspace = Workspace(name=name.strip(), kind=kind)
            session.add(workspace)
            session.flush()
            session.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
            return {"user_id": user.id, "workspace_id": workspace.id}

    _membership = staticmethod(membership)

    @staticmethod
    def require(session, actor, workspace_id, account_id=None, action="view"):
        return authorize(session, actor, action, workspace_id, account_id)

    def check_access(self, actor, workspace_id, account_id=None, action="view"):
        with self.db.transaction() as session:
            self.require(session, actor, workspace_id, account_id, action)

    @staticmethod
    def _ref(credential):
        return AccountRef(credential.workspace_id, credential.account_id, credential.generation, credential.version)

    def _authorized(self, session, account):
        if account is None:
            raise NotFound("Account is unavailable")
        credential = session.get(Credential, (account.workspace_id, account.id))
        if credential is None:
            raise SecretError("Account credential record is missing")
        ref = self._ref(credential)
        return AuthorizedAccount(ref, account.label, account.email, account.provider_subject,
                                 credential.expires_at, credential.refreshed_at, credential.invalid,
                                 self.cipher.unseal(credential.key_id, credential.ciphertext, ref))

    def authorized(self, workspace_id, account_id):
        with self.db.transaction() as session:
            account = session.get(Account, account_id)
            if account is None or account.workspace_id != workspace_id:
                raise NotFound("Account is unavailable")
            return self._authorized(session, account)

    def put_authorization(self, actor, workspace_id, *, email, subject, label, secrets,
                          expires_at=0, refreshed_at=None, invalid=False, expected=None, data=None, tags=()):
        with self.db.transaction(write=True) as session:
            self.require(session, actor, workspace_id, expected.account_id if expected else None, "manage")
            email, subject = normalize_email(email), normalize_subject(subject)
            if expected:
                account = session.get(Account, expected.account_id)
                credential = session.get(Credential, (workspace_id, account.id))
                if self._ref(credential) != expected:
                    raise Conflict("Account authorization changed; retry with current state")
                if account.provider_subject and account.provider_subject != subject:
                    raise Conflict("Authorization belongs to another provider identity")
                if account.email and account.email != email:
                    raise Conflict("Authorization belongs to another account email")
                previous = session.get(Snapshot, (workspace_id, account.id))
                if previous and previous.generation == expected.generation:
                    data = retain_own_limits(data, previous.data)
                session.execute(delete(Snapshot).where(Snapshot.workspace_id == workspace_id, Snapshot.account_id == account.id))
                session.execute(delete(Lease).where(Lease.workspace_id == workspace_id, Lease.account_id == account.id))
            else:
                duplicate = session.scalar(select(Account).where(Account.workspace_id == workspace_id,
                    Account.provider == "cursor", or_(Account.email == email if email else False,
                                                      Account.provider_subject == subject if subject else False)))
                if duplicate:
                    raise AuthorizationRejected("Account already exists in this workspace; use explicit reauthorization",
                                                "account_exists")
                account = Account(workspace_id=workspace_id, label=label or email or "Account",
                                  email=email, provider_subject=subject)
                session.add(account)
                session.flush()
                credential = Credential(workspace_id=workspace_id, account_id=account.id)
                session.add(credential)
            account.email, account.provider_subject = email, subject
            account.label = label or account.label
            account.updated_at = int(time.time())
            ref = AccountRef(workspace_id, account.id, new_id(), 1)
            credential.generation, credential.version = ref.generation, ref.version
            credential.key_id, credential.ciphertext = self.cipher.seal(secrets, ref)
            credential.expires_at = expires_at
            credential.refreshed_at = int(time.time()) if refreshed_at is None else refreshed_at
            credential.invalid = invalid
            self._set_tags(session, account, tags)
            if data is not None:
                now = int(time.time())
                session.add(Snapshot(workspace_id=workspace_id, account_id=account.id, generation=ref.generation,
                                     data=data, ok_at=now, attempted_at=now))
            audit(session, actor, "account.reauthorize" if expected else "account.create", workspace_id, account.id)
            session.flush()
            return self._authorized(session, account)

    @staticmethod
    def _set_tags(session, account, names):
        names = {name.strip() for name in names if name.strip()}
        if any(len(name) > 128 for name in names):
            raise Conflict("Tag name is too long")
        session.execute(delete(AccountTag).where(AccountTag.workspace_id == account.workspace_id,
                                                 AccountTag.account_id == account.id))
        for name in sorted(names):
            tag = session.scalar(select(Tag).where(Tag.workspace_id == account.workspace_id, Tag.name == name))
            if tag is None:
                tag = Tag(workspace_id=account.workspace_id, name=name)
                session.add(tag)
                session.flush()
            session.add(AccountTag(workspace_id=account.workspace_id, account_id=account.id, tag_id=tag.id))

    def edit(self, actor, workspace_id, account_id, *, label=None, tags=None, quota_reference=None):
        with self.db.transaction(write=True) as session:
            account = self.require(session, actor, workspace_id, account_id, "manage")
            if label is not None:
                if not label.strip():
                    raise Conflict("Account label cannot be empty")
                account.label = label.strip()
            if tags is not None:
                self._set_tags(session, account, tags)
            if quota_reference is not None:
                snapshot = session.get(Snapshot, (workspace_id, account.id))
                credential = session.get(Credential, (workspace_id, account.id))
                if snapshot is None or snapshot.generation != credential.generation:
                    raise Conflict("Refresh the account before setting quota references")
                snapshot.data = set_reference_limits(snapshot.data, quota_reference)
            account.updated_at = int(time.time())
            audit(session, actor, "account.edit", workspace_id, account_id,
                  changes={"fields": [name for name, value in (("label", label), ("tags", tags), ("quota_reference", quota_reference)) if value is not None]})

    def delete(self, actor, workspace_id, account_id):
        with self.db.transaction(write=True) as session:
            account = self.require(session, actor, workspace_id, account_id, "manage")
            session.delete(account)
            audit(session, actor, "account.delete", workspace_id, account_id)

    def list_views(self, actor, workspace_id):
        with self.db.transaction() as session:
            member = self._membership(session, actor, workspace_id)
            query = select(Account).where(Account.workspace_id == workspace_id).order_by(Account.id)
            if member.role not in {"owner", "admin"}:
                query = query.where(Account.id.in_(select(Grant.account_id).where(
                    Grant.workspace_id == workspace_id, Grant.user_id == actor.user_id)))
            result = []
            for account in session.scalars(query):
                credential = session.get(Credential, (workspace_id, account.id))
                snapshot = session.get(Snapshot, (workspace_id, account.id))
                if snapshot and snapshot.generation != credential.generation:
                    snapshot = None
                tags = list(session.scalars(select(Tag.name).join(AccountTag, (Tag.workspace_id == AccountTag.workspace_id)
                    & (Tag.id == AccountTag.tag_id)).where(AccountTag.workspace_id == workspace_id,
                                                         AccountTag.account_id == account.id).order_by(Tag.name)))
                result.append({"id": account.id, "workspace_id": workspace_id, "label": account.label,
                    "email": account.email, "tags": tags,
                    "capabilities": capabilities(session, actor, member, account.id), "authorization_generation": credential.generation,
                    "credential_version": credential.version, "auth_invalid": credential.invalid,
                    "expires_at": credential.expires_at, "refreshed_at": credential.refreshed_at,
                    "data": deepcopy(snapshot.data) if snapshot else None,
                    "ok_at": snapshot.ok_at if snapshot else 0,
                    "attempted_at": snapshot.attempted_at if snapshot else 0,
                    "error_kind": snapshot.error_kind if snapshot else None,
                    "error_message": snapshot.error_message if snapshot else None,
                    "failures": snapshot.failures if snapshot else 0})
            return result

    def claim_lease(self, workspace_id, account_id, owner, ttl):
        now = time.time()
        with self.db.transaction(write=True) as session:
            if session.scalar(select(Account.id).where(Account.id == account_id, Account.workspace_id == workspace_id)) is None:
                raise NotFound("Account is unavailable")
            statement = insert(Lease).values(workspace_id=workspace_id, account_id=account_id, owner=owner, expires_at=now + ttl)
            statement = statement.on_conflict_do_update(index_elements=["workspace_id", "account_id"],
                set_={"owner": owner, "expires_at": now + ttl}, where=Lease.expires_at <= now)
            return session.execute(statement).rowcount == 1

    def renew_lease(self, workspace_id, account_id, owner, ttl):
        with self.db.transaction(write=True) as session:
            lease = session.get(Lease, (workspace_id, account_id))
            if lease is None or lease.owner != owner or lease.expires_at <= time.time():
                return False
            lease.expires_at = time.time() + ttl
            return True

    def release_lease(self, workspace_id, account_id, owner):
        with self.db.transaction(write=True) as session:
            session.execute(delete(Lease).where(Lease.workspace_id == workspace_id, Lease.account_id == account_id, Lease.owner == owner))

    def rotate(self, account, secrets, expires_at, *, lease_owner, invalid=False, subject=None):
        ref = account.ref
        with self.db.transaction(write=True) as session:
            current = session.get(Credential, (ref.workspace_id, ref.account_id))
            lease = session.get(Lease, (ref.workspace_id, ref.account_id))
            if current is None or self._ref(current) != ref:
                return False
            if lease is None or lease.owner != lease_owner or lease.expires_at <= time.time():
                return False
            if subject:
                record = session.get(Account, ref.account_id)
                subject = normalize_subject(subject)
                if record.provider_subject and record.provider_subject != subject:
                    raise Conflict("Refreshed credentials belong to another identity")
                record.provider_subject = subject
            new = AccountRef(ref.workspace_id, ref.account_id, ref.generation, ref.version + 1)
            current.key_id, current.ciphertext = self.cipher.seal(secrets, new)
            current.version, current.expires_at = new.version, expires_at
            current.invalid = invalid
            if not invalid:
                current.refreshed_at = int(time.time())
            return True

    def record_snapshot(self, ref, *, data=None, error=None, actor=None):
        with self.db.transaction(write=True) as session:
            if actor is not None:
                self.require(session, actor, ref.workspace_id, ref.account_id, "use")
            current = session.get(Credential, (ref.workspace_id, ref.account_id))
            if current is None or current.generation != ref.generation:
                return False
            snapshot = session.get(Snapshot, (ref.workspace_id, ref.account_id))
            if snapshot is None or snapshot.generation != ref.generation:
                if snapshot:
                    session.delete(snapshot)
                    session.flush()
                snapshot = Snapshot(workspace_id=ref.workspace_id, account_id=ref.account_id,
                                    generation=ref.generation, ok_at=0, failures=0)
                session.add(snapshot)
            snapshot.attempted_at = int(time.time())
            if error:
                snapshot.failures = snapshot.failures + 1 if snapshot.error_kind == error[0] else 1
                snapshot.error_kind, snapshot.error_message = error
            else:
                snapshot.data = retain_own_limits(data, snapshot.data)
                snapshot.ok_at = snapshot.attempted_at
                snapshot.error_kind = snapshot.error_message = None
                snapshot.failures = 0
            if actor is not None:
                audit(session, actor, "account.refresh", ref.workspace_id, ref.account_id,
                      "failure" if error else "success")
            return True

    def verify(self):
        self.check_key()
        with self.db.transaction() as session:
            if session.connection().exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
                raise Conflict("Database integrity check failed")
            if session.connection().exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                raise Conflict("Database foreign key check failed")
            accounts = list(session.scalars(select(Account)))
            for account in accounts:
                self._authorized(session, account)
            return {"schema": session.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                    "accounts": len(accounts), "credentials_decryptable": len(accounts),
                    "workspaces": session.scalar(select(func.count()).select_from(Workspace)),
                    "imports": session.scalar(select(func.count()).select_from(LegacyImport))}
