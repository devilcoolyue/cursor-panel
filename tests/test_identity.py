from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select

from cursor_dashboard.api.app import create_app
from cursor_dashboard.application.security import Limiter, digest, password_hash, verify_password
from cursor_dashboard.domain.core import Actor, Conflict, CoreError, Forbidden, NotFound, Throttled, Unauthenticated
from cursor_dashboard.infrastructure.persistence.models import (AuditEvent, Base, Grant, Invitation,
    Membership, SwitchTicket, User, UserSession)
from test_core import CoreFixture, data, token

PASSWORD = "Synthetic test password 42!"


class IdentityFixture(CoreFixture):
    def setUp(self):
        super().setUp()
        self.identity, self.spaces = self.core.identity, self.core.workspaces
        self.identity.initialize_server("first@example.test", PASSWORD)

    def sign_in(self):
        return self.identity.login("first@example.test", PASSWORD)

    def join(self, login="member@example.test", role="member", actor=None):
        invitation = self.spaces.invite(self.actor, self.first, login, role)
        result = self.spaces.accept(invitation["token"], actor=actor, password=None if actor else PASSWORD)
        return Actor(result["user_id"])


class IdentityTest(IdentityFixture, unittest.TestCase):
    def test_initialization_is_explicit_and_preserves_p1_owner(self):
        self.assertTrue(self.identity.initialized())
        me = self.identity.me(self.actor)
        self.assertTrue(me["instance_admin"])
        self.assertEqual(sum(s["kind"] == "personal" for s in me["workspaces"]), 1)
        self.assertIn(self.first, {s["id"] for s in me["workspaces"]})
        with self.assertRaises(Conflict):
            self.identity.initialize_server("other@example.test", PASSWORD)
        with self.assertRaises(CoreError):
            create_app(self.core, public_origin="http://localhost:8000")
        self.core.config = replace(self.config, mode="server")
        with self.assertRaises(CoreError):
            create_app(self.core, public_origin="http://example.test")
        with self.core.db.transaction(write=True) as session:
            session.get(User, self.actor.user_id).instance_admin = False
        with self.assertRaises(CoreError):
            create_app(self.core, public_origin="https://example.test")

    def test_passwords_and_session_tickets_are_not_stored_in_plaintext(self):
        logged = self.sign_in()
        self.assertEqual(self.identity.authenticate(logged.token, csrf=logged.csrf).user_id, self.actor.user_id)
        self.assertNotIn(logged.token, repr(logged))
        with self.core.db.transaction() as session:
            user = session.get(User, self.actor.user_id)
            self.assertTrue(verify_password(PASSWORD, user.password_hash))
            self.assertFalse(verify_password("wrong", user.password_hash))
            self.assertNotEqual(password_hash(PASSWORD), user.password_hash)
            row = session.get(UserSession, logged.actor.session_id)
            self.assertEqual(row.token_hash, digest(logged.token))
        disk = b"".join(p.read_bytes() for p in self.config.data_dir.glob("core.db*"))
        for secret in (PASSWORD, logged.token, logged.csrf):
            self.assertNotIn(secret.encode(), disk)
        with self.assertRaises(Forbidden):
            self.identity.authenticate(logged.token, csrf="bad")
        for login in ("first@example.test", "missing@example.test"):
            with self.assertRaisesRegex(Unauthenticated, "Invalid login or password"):
                self.identity.login(login, "bad")

    def test_password_change_revokes_all_sessions_and_recovery_is_audited(self):
        first, second = self.sign_in(), self.sign_in()
        self.identity.change_password(first.actor, PASSWORD, "Replacement password 99!")
        for logged in (first, second):
            with self.assertRaises(Unauthenticated):
                self.identity.authenticate(logged.token)
            with self.assertRaises(Unauthenticated):
                self.core.accounts.list(logged.actor, self.first)
        self.identity.recover_password("first@example.test", PASSWORD)
        self.sign_in()
        actions = {e["action"] for e in self.spaces.audits(self.actor, None)}
        self.assertTrue({"user.password", "operator.password_recovery"} <= actions)

    def test_session_revocation_expiry_and_cross_user_revocation(self):
        logged = self.sign_in()
        with self.assertRaises(NotFound):
            self.identity.revoke_session(self.other, logged.actor.session_id)
        self.identity.revoke_session(logged.actor, logged.actor.session_id)
        with self.assertRaises(Unauthenticated):
            self.identity.me(logged.actor)
        logged = self.sign_in()
        with self.core.db.transaction(write=True) as session:
            session.get(UserSession, logged.actor.session_id).expires_at = 0
        with self.assertRaises(Unauthenticated):
            self.identity.authenticate(logged.token)

    def test_invitation_single_use_identity_binding_and_personal_isolation(self):
        own = self.account()
        invitation = self.spaces.invite(self.actor, self.first, "new@example.test", "member")
        new = self.spaces.accept(invitation["token"], password=PASSWORD)
        actor = Actor(new["user_id"])
        self.assertEqual(self.core.accounts.list(actor, self.first), [])
        personal = next(s["id"] for s in self.identity.me(actor)["workspaces"] if s["kind"] == "personal")
        with self.assertRaises(NotFound):
            self.core.accounts.list(self.actor, personal)
        with self.assertRaises(NotFound):
            self.spaces.accept(invitation["token"], actor=actor)
        with self.core.db.transaction() as session:
            record = session.get(Invitation, invitation["id"])
            self.assertEqual(record.token_hash, digest(invitation["token"]))
        # Inviting an existing owner into another team cannot take over its identity.
        invitation = self.spaces.invite(self.other, self.second, "first@example.test", "viewer")
        with self.assertRaises(Unauthenticated):
            self.spaces.accept(invitation["token"], password="Attacker reset password!")
        with self.assertRaises(Unauthenticated):
            self.spaces.accept(invitation["token"], actor=actor)
        self.spaces.accept(invitation["token"], actor=self.actor)
        self.assertEqual(self.core.accounts.get(self.actor, self.first, own.ref.account_id)["id"], own.ref.account_id)
        self.sign_in()

    def test_expired_revoked_and_issuer_downgraded_invites(self):
        for change in ("expire", "revoke", "demote", "remove", "disable"):
            with self.subTest(change=change):
                admin = self.join(f"admin-{change}@example.test", "admin")
                invitation = self.spaces.invite(admin, self.first, f"invite-{change}@example.test", "member")
                if change == "expire":
                    with self.core.db.transaction(write=True) as session:
                        session.get(Invitation, invitation["id"]).expires_at = 0
                elif change == "revoke":
                    self.spaces.revoke_invitation(admin, self.first, invitation["id"])
                elif change == "demote":
                    self.spaces.set_role(self.actor, self.first, admin.user_id, "member")
                    self.spaces.set_role(self.actor, self.first, admin.user_id, "admin")
                elif change == "remove":
                    self.spaces.remove_member(self.actor, self.first, admin.user_id)
                else:
                    self.identity.set_user_active(self.actor, admin.user_id, False)
                with self.assertRaises(NotFound):
                    self.spaces.accept(invitation["token"])

    def test_owner_transfer_personal_rules_and_admin_limits(self):
        admin = self.join("admin@example.test", "admin")
        member = self.join()
        with self.assertRaises(Forbidden):
            self.spaces.set_role(admin, self.first, member.user_id, "admin")
        with self.assertRaises(Forbidden):
            self.spaces.invite(admin, self.first, "bad@example.test", "admin")
        with self.assertRaises(Conflict):
            self.spaces.remove_member(self.actor, self.first, self.actor.user_id)
        with self.assertRaises(Forbidden):
            self.spaces.transfer(admin, self.first, member.user_id)
        personal = next(s["id"] for s in self.identity.me(self.actor)["workspaces"] if s["kind"] == "personal")
        for operation in (lambda: self.spaces.invite(self.actor, personal, "bad@example.test"),
                          lambda: self.spaces.transfer(self.actor, personal, member.user_id),
                          lambda: self.spaces.delete(self.actor, personal)):
            with self.assertRaises(Forbidden):
                operation()
        self.spaces.transfer(self.actor, self.first, member.user_id)
        with self.core.db.transaction() as session:
            self.assertEqual(session.get(Membership, (self.first, member.user_id)).role, "owner")
            self.assertEqual(session.get(Membership, (self.first, self.actor.user_id)).role, "admin")
            self.assertEqual(session.scalar(select(func.count()).select_from(Membership).where(
                Membership.workspace_id == self.first, Membership.role == "owner")), 1)
        self.spaces.remove_member(admin, self.first, admin.user_id)  # self-leave allowed
        self.spaces.delete(member, self.first)
        with self.assertRaises(NotFound):
            self.core.accounts.list(member, self.first)
        with self.core.db.transaction() as session:
            self.assertIsNotNone(session.scalar(select(AuditEvent).where(AuditEvent.action == "workspace.delete")))

    def test_audit_failure_rolls_back_business_change_and_owner_transfer(self):
        member = self.join()
        account = self.account()
        with patch("cursor_dashboard.application.identity.audit", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, "use")
            with self.assertRaises(RuntimeError):
                self.spaces.transfer(self.actor, self.first, member.user_id)
        with self.core.db.transaction() as session:
            self.assertIsNone(session.get(Grant, (self.first, account.ref.account_id, member.user_id)))
            self.assertEqual(session.get(Membership, (self.first, self.actor.user_id)).role, "owner")
        with patch("cursor_dashboard.infrastructure.persistence.repository.audit", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.core.accounts.delete(self.actor, self.first, account.ref.account_id)
        self.core.accounts.get(self.actor, self.first, account.ref.account_id)

    def test_instance_admin_cannot_read_other_personal_space_or_workspace_audit(self):
        member = self.join()
        personal = next(s["id"] for s in self.identity.me(member)["workspaces"] if s["kind"] == "personal")
        self.account(actor=member, workspace=personal)
        with self.assertRaises(NotFound):
            self.core.accounts.list(self.actor, personal)
        with self.assertRaises(NotFound):
            self.spaces.audits(self.actor, personal)
        with self.assertRaises(Forbidden):
            self.identity.users(member)
        with self.assertRaises(Conflict):
            self.identity.set_user_active(self.actor, self.actor.user_id, False)
        self.identity.set_user_active(self.actor, member.user_id, False)
        with self.assertRaises(NotFound):
            self.identity.me(member)
        self.identity.set_user_active(self.actor, member.user_id, True)
        self.assertEqual(self.identity.me(member)["id"], member.user_id)

    def test_role_grant_and_capability_matrix(self):
        account = self.account(snapshot=data(42))
        for role in ("admin", "member", "viewer"):
            actor = self.join(f"{role}@example.test", role)
            for level in (None, "view", "use"):
                with self.subTest(role=role, level=level):
                    if role != "admin":
                        if role == "viewer" and level == "use":
                            with self.assertRaises(Conflict):
                                self.spaces.set_grant(self.actor, self.first, account.ref.account_id, actor.user_id, level)
                            continue
                        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, actor.user_id, level)
                    visible = role == "admin" or level is not None
                    usable = role == "admin" or (role == "member" and level == "use")
                    rows = self.core.accounts.search(actor, self.first)
                    self.assertEqual(rows["total"], int(visible))
                    if visible:
                        caps = rows["items"][0]["capabilities"]
                        self.assertEqual(caps["switch"], usable)
                        self.assertEqual(caps["edit"], role == "admin")
                    for action, allowed in (("view", visible), ("use", usable), ("manage", role == "admin"),
                                            ("grant", role == "admin")):
                        if allowed:
                            self.repo.check_access(actor, self.first, account.ref.account_id, action)
                        else:
                            with self.assertRaises((NotFound, Forbidden)):
                                self.repo.check_access(actor, self.first, account.ref.account_id, action)
        with self.assertRaises(Forbidden):
            self.repo.check_access(self.actor, self.first, account.ref.account_id, "unknown-action")

    def test_visible_filtering_precedes_search_pagination_and_tag_counts(self):
        visible = self.account(snapshot=data(42))
        hidden = self.account(email="hidden@example.test", marker="hidden", snapshot=data(500))
        self.repo.edit(self.actor, self.first, visible.ref.account_id, tags=["visible"])
        self.repo.edit(self.actor, self.first, hidden.ref.account_id, tags=["private"])
        member = self.join()
        self.spaces.set_grant(self.actor, self.first, visible.ref.account_id, member.user_id, "view")
        result = self.core.accounts.search(member, self.first, limit=1, offset=1)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["tags"], {"visible": 1})
        self.assertEqual(self.core.accounts.search(member, self.first, query="hidden")["total"], 0)
        self.assertEqual(self.core.accounts.search(member, self.first, tag="private")["total"], 0)
        with self.assertRaises(NotFound):
            self.spaces.set_grant(self.actor, self.first, visible.ref.account_id, self.other.user_id, "view")
        with self.assertRaises(NotFound):
            self.core.accounts.edit(self.other, self.second, visible.ref.account_id, tags=["malicious"])
        with self.assertRaises(Forbidden):
            self.core.accounts.edit(member, self.first, visible.ref.account_id, tags=["malicious"])

    def test_limits_are_bounded_and_fail_closed(self):
        limiter = Limiter(capacity=1)
        limiter.check(("one", 1, 60))
        with self.assertRaises(Throttled):
            limiter.check(("one", 1, 60))
        with self.assertRaises(Throttled):
            limiter.check(("two", 1, 60))
        for _ in range(8):
            with self.assertRaises(Unauthenticated):
                self.identity.login("unknown@example.test", "wrong")
        with self.assertRaises(Throttled):
            self.identity.login("unknown@example.test", "wrong")
        with self.core.db.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), Base.metadata), [])


class SwitchAndRaceTest(IdentityFixture, unittest.IsolatedAsyncioTestCase):
    async def ticket(self, actor=None, account=None):
        account = account or self.account()
        logged = actor or self.sign_in().actor
        ticket = await self.core.switches.issue(logged, self.first, account.ref.account_id)
        return logged, account, ticket

    async def test_ticket_single_use_session_binding_and_transactional_audit(self):
        actor, account, ticket = await self.ticket()
        other_session = self.sign_in().actor
        with self.assertRaises(NotFound):
            self.core.switches.consume(other_session, ticket["token"])
        self.assertLessEqual(ticket["expires_at"], account.expires_at)
        self.assertLessEqual(ticket["expires_at"], time.time() + 300)
        with patch("cursor_dashboard.application.switching.audit", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.core.switches.consume(actor, ticket["token"])
        def consume():
            try:
                return self.core.switches.consume(actor, ticket["token"])
            except NotFound:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: consume(), range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)
        delivered = next(r for r in results if r is not None)
        self.assertEqual(delivered.secrets, account.secrets)
        self.assertNotIn(account.secrets.access_token, repr(delivered))
        self.core.switches.record_result(actor, delivered.ticket_id, "failure")
        actions = {e["action"] for e in self.spaces.audits(actor, self.first)}
        self.assertTrue({"switch.issue", "switch.consume", "switch.result"} <= actions)
        serialized = json.dumps(self.spaces.audits(actor, self.first))
        self.assertNotIn(ticket["token"], serialized)
        self.assertNotIn(account.secrets.refresh_token, serialized)

    async def test_ticket_invalid_after_session_revoke_expiry_rotation_reauthorization_or_delete(self):
        account = self.account()
        for change in ("session", "expire", "rotate", "reauthorize", "delete"):
            with self.subTest(change=change):
                logged = self.sign_in()
                ticket = await self.core.switches.issue(logged.actor, self.first, account.ref.account_id)
                if change == "session":
                    self.identity.revoke_session(logged.actor, logged.actor.session_id)
                elif change == "expire":
                    with self.core.db.transaction(write=True) as session:
                        row = session.scalar(select(SwitchTicket).where(SwitchTicket.token_hash == digest(ticket["token"])))
                        row.expires_at = 0
                elif change == "rotate":
                    self.repo.claim_lease(self.first, account.ref.account_id, "test", 30)
                    self.repo.rotate(account, account.secrets, account.expires_at, lease_owner="test")
                    self.repo.release_lease(self.first, account.ref.account_id, "test")
                elif change == "reauthorize":
                    current = self.repo.authorized(self.first, account.ref.account_id)
                    self.repo.put_authorization(self.actor, self.first, email=current.email, subject=current.subject,
                        label=current.label, secrets=current.secrets, expected=current.ref, expires_at=current.expires_at)
                else:
                    self.core.accounts.delete(self.actor, self.first, account.ref.account_id)
                with self.assertRaises((NotFound, Unauthenticated)):
                    self.core.switches.consume(logged.actor, ticket["token"])

    async def test_downgrade_revokes_use_and_unclaimed_tickets_even_after_restore(self):
        member = self.join()
        logged = self.identity.login("member@example.test", PASSWORD)
        account = self.account()
        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, "use")
        ticket = await self.core.switches.issue(logged.actor, self.first, account.ref.account_id)
        self.spaces.set_role(self.actor, self.first, member.user_id, "viewer")
        with self.core.db.transaction() as session:
            self.assertEqual(session.get(Grant, (self.first, account.ref.account_id, member.user_id)).level, "view")
        self.spaces.set_role(self.actor, self.first, member.user_id, "member")
        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, "use")
        with self.assertRaises(NotFound):
            self.core.switches.consume(logged.actor, ticket["token"])
        ticket = await self.core.switches.issue(logged.actor, self.first, account.ref.account_id)
        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, None)
        with self.assertRaises(NotFound):
            self.core.switches.consume(logged.actor, ticket["token"])

    async def test_slow_ticket_issue_rechecks_permission_after_refresh(self):
        member = self.join()
        logged = self.identity.login("member@example.test", PASSWORD)
        account = self.account(expiry=int(time.time()) + 1)
        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, "use")
        async def refresh(*args):
            self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, None)
            return {"access_token": token("new"), "refresh_token": "synthetic-new-refresh"}
        self.core.credentials.gateway = refresh
        with self.assertRaises(NotFound):
            await self.core.switches.issue(logged.actor, self.first, account.ref.account_id)
        with self.core.db.transaction() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(SwitchTicket)), 0)

    async def test_slow_detail_and_refresh_reject_revoked_session(self):
        account = self.account(snapshot=data(42))
        for operation in ("detail", "refresh"):
            logged = self.sign_in()
            async def fetch(cookie, label, name, *args, logged=logged):
                self.identity.revoke_session(self.actor, logged.actor.session_id)
                return {"email": account.email, "authId": account.subject} if name == "desktop_me" else {}
            self.core.credentials.gateway = fetch
            with self.assertRaises(Unauthenticated):
                await getattr(self.core.accounts, operation)(logged.actor, self.first, account.ref.account_id)
            self.assertEqual(len(self.core.accounts._details), 0)
        self.assertEqual(self.core.accounts.get(self.actor, self.first, account.ref.account_id)["data"]["quota"]["overall"]["limit_usd"], 42)

    async def test_queued_refresh_rechecks_permissions_before_network(self):
        account = self.account(snapshot=data(42))
        logged = self.sign_in()
        async with self.core.accounts._account_lock((self.first, account.ref.account_id)):
            waiting = asyncio.create_task(self.core.accounts.refresh(logged.actor, self.first, account.ref.account_id))
            await asyncio.sleep(0)
            self.identity.revoke_session(self.actor, logged.actor.session_id)
        with self.assertRaises(Unauthenticated):
            await waiting

    async def test_unauthorized_member_cannot_start_browser_authorization(self):
        account = self.account()
        member = self.join()
        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, member.user_id, "use")
        for account_id in (None, account.ref.account_id):
            with self.assertRaises(Forbidden):
                await self.core.accounts.begin_authorization(member, self.first, account_id=account_id)
        self.assertEqual(self.core.accounts._authorizations, {})
        self.assertEqual(self.repo.authorized(self.first, account.ref.account_id).ref, account.ref)
