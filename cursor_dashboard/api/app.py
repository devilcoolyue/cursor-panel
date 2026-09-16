from __future__ import annotations

import re
import asyncio
from contextlib import asynccontextmanager, suppress
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import models as dto
from .manual_switch import render_script
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..application.queries import QueryFailure
from ..application.security import digest
from ..client import AuthExpired, RateLimited
from ..desktop import CookieSessionError, DesktopSessionError
from ..domain.core import (CoreError, Forbidden, NotFound, SecretError,
                            Throttled, Unauthenticated)
from ..infrastructure.persistence.policy import audit
from ..switch_links import download_command

import requests


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallUpdate(Input):
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class Login(Input):
    login: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=256)


class PasswordChange(Input):
    current_password: SecretStr = Field(max_length=256)
    new_password: SecretStr = Field(min_length=12, max_length=256)


class WorkspaceCreate(Input):
    name: str = Field(min_length=1, max_length=128)


class Invite(Input):
    login: str = Field(min_length=1, max_length=320)
    role: str = Field(pattern="^(admin|member|viewer)$")


class AcceptInvite(Input):
    token: SecretStr = Field(min_length=20, max_length=128)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=256)


class RoleChange(Input):
    role: str = Field(pattern="^(admin|member|viewer)$")


class OwnerChange(Input):
    user_id: uuid.UUID


class GrantChange(Input):
    level: str = Field(pattern="^(view|use)$")


class UserState(Input):
    active: bool = Field(strict=True)


class QuotaReference(Input):
    cycle_start: str = Field(max_length=64)
    cursor_models: float | None = Field(default=None, gt=0, le=1e9, strict=True, allow_inf_nan=False)
    other_models: float | None = Field(default=None, gt=0, le=1e9, strict=True, allow_inf_nan=False)
    overall: float | None = Field(default=None, gt=0, le=1e9, strict=True, allow_inf_nan=False)


class AccountEdit(Input):
    label: str | None = Field(default=None, min_length=1, max_length=256)
    tags: list[str] | None = Field(default=None, max_length=100)
    quota_reference: QuotaReference | None = None


class Authorization(Input):
    cookie: SecretStr = Field(min_length=1, max_length=16384)
    label: str | None = Field(default=None, min_length=1, max_length=256)
    tags: list[str] = Field(default_factory=list, max_length=100)


class SwitchPlatform(Input):
    platform: Literal["macos", "windows"]


class ManualConsume(SwitchPlatform):
    token: SecretStr = Field(min_length=20, max_length=128)


class DeviceApproval(Input):
    code_challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")
    callback: str = Field(max_length=256)
    device_id: uuid.UUID
    device_name: str = Field(min_length=1, max_length=128)


class DeviceExchange(Input):
    code: SecretStr = Field(min_length=20, max_length=128)
    verifier: SecretStr = Field(min_length=43, max_length=128)
    callback: str = Field(max_length=256)
    device_id: uuid.UUID


class DeviceConsume(Input):
    token: SecretStr = Field(min_length=20, max_length=128)


class DeviceResult(Input):
    result: Literal["success", "failure", "cancelled"]


def validate_origin(public_origin):
    parsed = urlsplit(public_origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise CoreError("Public origin must be an http(s) origin without path or credentials")
    try:
        _ = parsed.port
    except ValueError:
        raise CoreError("Public origin has an invalid port") from None
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise CoreError("External server origins require HTTPS")
    return parsed


def create_app(core, *, public_origin, web_dir=None, manual_switch_preview=False, _local=None,
               _device_switch_test=False):
    parsed = validate_origin(public_origin)
    if _local is not None and (core.config.mode != "local" or _local.core is not core):
        raise CoreError("Private desktop identity requires its own local core")
    if _local is None and core.config.mode != "server":
        raise CoreError("HTTP requires explicit server mode")
    if _local is None and not core.identity.initialized():
        raise CoreError("Initialize server authentication with cursor-core server-init before listening")
    secure = parsed.scheme == "https"
    cookie_name = "__Host-cursor_session" if secure else "cursor_session"
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(core.retention.serve())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Cursor Panel V2", version="1", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.core = core
    from cursor_dashboard import __version__
    from cursor_dashboard.updates import releases
    from cursor_dashboard.updates.control import UpdateControl
    from cursor_dashboard.infrastructure.persistence.policy import authorize
    update_control = UpdateControl.from_env() if _local is None else UpdateControl()

    def error_response(status, message):
        return JSONResponse({"detail": message}, status_code=status)

    @app.middleware("http")
    async def boundary(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        origin = request.headers.get("origin")
        native = (origin is None and not any(key.startswith("sec-fetch-") for key in request.headers)
                  and not request.headers.get("cookie") and
                  (request.headers.get("authorization", "").startswith("Bearer ") or
                   request.url.path == "/api/v1/auth/devices/exchange"))
        if _local is not None:
            # The outer native middleware authenticates every request, including reads.
            response = await call_next(request)
        elif request.headers.get("host", "").lower() != parsed.netloc.lower():
            response = error_response(400, "Invalid host")
        elif (origin is not None and origin != public_origin) or request.headers.get("sec-fetch-site") == "cross-site":
            response = error_response(403, "Invalid origin")
        elif request.method not in {"GET", "HEAD", "OPTIONS"} and origin != public_origin and not native:
            response = error_response(403, "Same-origin requests are required")
        else:
            length = request.headers.get("content-length", "0")
            if not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > 65536:
                response = error_response(413, "Request body is too large")
            else:
                # Bound chunked input as well as Content-Length before JSON parsing.
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        break
                if len(body) > 65536:
                    response = error_response(413, "Request body is too large")
                else:
                    request._body = bytes(body)
                    response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(CoreError)
    async def core_error(request, error):
        status = (401 if isinstance(error, Unauthenticated) else 403 if isinstance(error, Forbidden)
                  else 404 if isinstance(error, NotFound) else 429 if isinstance(error, Throttled)
                  else 503 if isinstance(error, SecretError) else 409)
        # Failed attempts contain only server-defined route identifiers and UUIDs.
        actor = getattr(request.state, "actor", None)
        route = request.scope.get("route")
        if actor and status in {401, 403, 404}:
            workspace = request.path_params.get("workspace_id")
            try:
                workspace = str(uuid.UUID(str(workspace))) if workspace else None
            except ValueError:
                workspace = None
            with core.db.transaction(write=True) as session:
                audit(session, actor, "http." + (route.name if route else "request"), workspace, result="denied")
        return error_response(status, str(error))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default includes rejected input (including Cookie/password).
        return error_response(422, "Invalid request fields")

    @app.exception_handler(CookieSessionError)
    async def cookie_error(request, error):
        return JSONResponse({"detail": str(error), "code": error.code}, status_code=422)

    async def provider_error(request, error):
        return error_response(502, "Provider request failed; retry or reauthorize the account")

    for error_type in (QueryFailure, AuthExpired, RateLimited, DesktopSessionError, requests.RequestException):
        app.add_exception_handler(error_type, provider_error)

    def current_actor(request: Request):
        if _local is not None:
            actor = _local.identity.actor(request.state.request_id)
            request.state.actor = actor
            return actor
        if request.headers.get("authorization"):
            supplied = request.headers.get("authorization", "")
            if not supplied.startswith("Bearer "):
                raise Unauthenticated("Device authentication required")
            actor = core.identity.authenticate(supplied[7:], kind="device", request_id=request.state.request_id)
            if (request.headers.get("origin") is not None or request.headers.get("cookie")
                    or any(key.startswith("sec-fetch-") for key in request.headers)):
                raise Forbidden("Device authentication requires the native channel")
            request.state.actor = actor
            return actor
        token = request.cookies.get(cookie_name, "")
        csrf = request.headers.get("x-csrf-token", "") if request.method not in {"GET", "HEAD", "OPTIONS"} else None
        actor = core.identity.authenticate(token, csrf=csrf, request_id=request.state.request_id)
        request.state.actor = actor
        return actor

    def browser_actor(actor=Depends(current_actor)):
        core.devices.require_kind(actor, "web")
        return actor

    def device_actor(actor=Depends(current_actor)):
        core.devices.require_kind(actor, "device")
        return actor

    def switch_device(actor=Depends(device_actor)):
        # No production setting enables shared Cursor credentials before the
        # upstream S01–S06 matrix is verified. This hook is only used by fixtures.
        if not _device_switch_test:
            raise Forbidden("Remote switching awaits upstream session verification")
        return actor

    @app.get("/api/v1/bootstrap", response_model=dto.Bootstrap)
    def bootstrap():
        if _local is not None:
            return {"mode": "local", "initialized": True, "api_version": 1, "app_version": __version__,
                    "capabilities": {"workspaces": False, "invitations": False,
                        "manual_switch": False, "native_switch": True, "archives": True,
                        "device_sessions": False, "remote_switch": False}}
        return {"mode": "server", "initialized": True, "api_version": 1, "app_version": __version__,
                "capabilities": {"workspaces": True, "invitations": True,
                                 "manual_switch": True, "device_sessions": True,
                                 "remote_switch": bool(_device_switch_test)}}

    @app.get("/api/v1/updates", response_model=dto.ReleaseInfo)
    def check_updates(actor=Depends(current_actor)):
        try:
            return releases.check_release(server=_local is None)
        except releases.UpdateError:
            return error_response(503, "Release information is temporarily unavailable")

    @app.get("/api/v1/instance/update", response_model=dto.UpdateStatus)
    def update_status(actor=Depends(browser_actor)):
        with core.db.transaction() as session:
            authorize(session, actor, "instance")
        return update_control.status()

    @app.post("/api/v1/instance/update", response_model=dto.UpdateStatus, status_code=202)
    def install_update(body: InstallUpdate, actor=Depends(browser_actor)):
        with core.db.transaction() as session:
            authorize(session, actor, "instance")
        try:
            release = releases.check_release(server=True)
            if not release["available"] or not release["installable"] or release["latest_version"] != body.version:
                return error_response(409, "The requested update is no longer available")
            with core.db.transaction(write=True) as session:
                authorize(session, actor, "instance")
                result = update_control.enqueue(body.version)
                audit(session, actor, "instance.update.requested")
        except releases.UpdateError:
            return error_response(409, "The update service is not ready; check again before retrying")
        return result

    @app.post("/api/v1/auth/login", response_model=dto.LoginResult)
    def login(body: Login, request: Request):
        result = core.identity.login(body.login, body.password.get_secret_value(),
            source=request.client.host if request.client else "unknown", request_id=request.state.request_id)
        response = JSONResponse({"csrf_token": result.csrf, "expires_at": result.expires_at})
        response.set_cookie(cookie_name, result.token, max_age=core.identity.session_ttl,
                            httponly=True, secure=secure, samesite="strict", path="/")
        return response

    @app.get("/api/v1/auth/csrf", response_model=dto.Csrf)
    def csrf(request: Request, actor=Depends(browser_actor)):
        return {"csrf_token": digest("csrf:" + request.cookies[cookie_name])}

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(actor=Depends(current_actor)):
        core.identity.revoke_session(actor, actor.session_id)
        response = Response(status_code=204)
        response.delete_cookie(cookie_name, secure=secure, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/v1/me", response_model=dto.Me)
    def me(actor=Depends(current_actor)):
        return core.identity.me(actor)

    @app.put("/api/v1/auth/password", status_code=204)
    def password(body: PasswordChange, actor=Depends(current_actor)):
        core.identity.change_password(actor, body.current_password.get_secret_value(), body.new_password.get_secret_value())
        return Response(status_code=204)

    @app.get("/api/v1/auth/sessions", response_model=list[dto.SessionView])
    def sessions(actor=Depends(current_actor)):
        return core.identity.sessions(actor)

    @app.delete("/api/v1/auth/sessions/{session_id}", status_code=204)
    def revoke_session(session_id: uuid.UUID, actor=Depends(current_actor)):
        core.identity.revoke_session(actor, str(session_id))
        return Response(status_code=204)

    @app.post("/api/v1/auth/devices/authorize", response_model=dto.DeviceApproved)
    def approve_device(body: DeviceApproval, actor=Depends(browser_actor)):
        return core.devices.authorize(actor, **{**body.model_dump(), "device_id": str(body.device_id)})

    @app.post("/api/v1/auth/devices/exchange", response_model=dto.DeviceLogin)
    def exchange_device(body: DeviceExchange, request: Request):
        if (request.headers.get("origin") is not None or request.headers.get("cookie")
                or any(key.startswith("sec-fetch-") for key in request.headers)):
            raise Forbidden("Device exchange requires the native channel")
        return core.devices.exchange(code=body.code.get_secret_value(), verifier=body.verifier.get_secret_value(),
            callback=body.callback, device_id=str(body.device_id), request_id=request.state.request_id,
            source=request.client.host if request.client else "unknown")

    @app.get("/api/v1/auth/devices", response_model=list[dto.SessionView])
    def devices(actor=Depends(current_actor)):
        return core.devices.sessions(actor)

    @app.delete("/api/v1/auth/devices/{session_id}", status_code=204)
    def revoke_device(session_id: uuid.UUID, actor=Depends(current_actor)):
        core.devices.revoke(actor, str(session_id))
        return Response(status_code=204)

    @app.post("/api/v1/workspaces", status_code=201, response_model=dto.WorkspaceCreated)
    def create_workspace(body: WorkspaceCreate, actor=Depends(current_actor)):
        return core.workspaces.create(actor, body.name)

    @app.delete("/api/v1/workspaces/{workspace_id}", status_code=204)
    def delete_workspace(workspace_id: uuid.UUID, actor=Depends(current_actor)):
        core.workspaces.delete(actor, str(workspace_id))
        return Response(status_code=204)

    @app.put("/api/v1/workspaces/{workspace_id}/owner", status_code=204)
    def transfer_owner(workspace_id: uuid.UUID, body: OwnerChange, actor=Depends(current_actor)):
        core.workspaces.transfer(actor, str(workspace_id), str(body.user_id))
        return Response(status_code=204)

    @app.get("/api/v1/workspaces/{workspace_id}/members", response_model=list[dto.MemberView])
    def members(workspace_id: uuid.UUID, actor=Depends(current_actor)):
        return core.workspaces.members(actor, str(workspace_id))

    @app.put("/api/v1/workspaces/{workspace_id}/members/{user_id}", status_code=204)
    def role(workspace_id: uuid.UUID, user_id: uuid.UUID, body: RoleChange, actor=Depends(current_actor)):
        core.workspaces.set_role(actor, str(workspace_id), str(user_id), body.role)
        return Response(status_code=204)

    @app.delete("/api/v1/workspaces/{workspace_id}/members/{user_id}", status_code=204)
    def remove_member(workspace_id: uuid.UUID, user_id: uuid.UUID, actor=Depends(current_actor)):
        core.workspaces.remove_member(actor, str(workspace_id), str(user_id))
        return Response(status_code=204)

    @app.post("/api/v1/workspaces/{workspace_id}/invitations", status_code=201, response_model=dto.InvitationIssued)
    def invite(workspace_id: uuid.UUID, body: Invite, actor=Depends(current_actor)):
        return core.workspaces.invite(actor, str(workspace_id), body.login, body.role)

    @app.get("/api/v1/workspaces/{workspace_id}/invitations", response_model=list[dto.InvitationView])
    def invitations(workspace_id: uuid.UUID, actor=Depends(current_actor)):
        return core.workspaces.invitations(actor, str(workspace_id))

    @app.delete("/api/v1/workspaces/{workspace_id}/invitations/{invitation_id}", status_code=204)
    def revoke_invitation(workspace_id: uuid.UUID, invitation_id: uuid.UUID, actor=Depends(current_actor)):
        core.workspaces.revoke_invitation(actor, str(workspace_id), str(invitation_id))
        return Response(status_code=204)

    @app.post("/api/v1/invitations/accept", response_model=dto.Joined)
    def accept(body: AcceptInvite, request: Request):
        actor = current_actor(request) if request.cookies.get(cookie_name) else None
        return core.workspaces.accept(body.token.get_secret_value(), actor=actor,
            password=body.password.get_secret_value() if body.password else None,
            request_id=request.state.request_id, source=request.client.host if request.client else "unknown")

    @app.get("/api/v1/workspaces/{workspace_id}/accounts", response_model=dto.AccountPage)
    def accounts(workspace_id: uuid.UUID, q: str = Query("", max_length=256), tag: str | None = Query(None, max_length=128),
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), actor=Depends(current_actor)):
        return core.accounts.search(actor, str(workspace_id), query=q, tag=tag, limit=limit, offset=offset)

    @app.get("/api/v1/workspaces/{workspace_id}/accounts/{account_id}", response_model=dto.AccountView)
    def account(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(current_actor)):
        return core.accounts.get(actor, str(workspace_id), str(account_id))

    @app.post("/api/v1/workspaces/{workspace_id}/accounts", status_code=201, response_model=dto.AccountView,
              responses={422: {"model": dto.AuthorizationError}})
    async def authorize_account(workspace_id: uuid.UUID, body: Authorization, actor=Depends(current_actor)):
        return await core.accounts.authorize(actor, str(workspace_id), body.cookie.get_secret_value(),
                                             label=body.label, tags=body.tags)

    @app.post("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/authorization", response_model=dto.AccountView,
              responses={422: {"model": dto.AuthorizationError}})
    async def reauthorize_account(workspace_id: uuid.UUID, account_id: uuid.UUID, body: Authorization,
                                  actor=Depends(current_actor)):
        return await core.accounts.authorize(actor, str(workspace_id), body.cookie.get_secret_value(),
                                             label=body.label, tags=body.tags, account_id=str(account_id))

    @app.patch("/api/v1/workspaces/{workspace_id}/accounts/{account_id}", response_model=dto.AccountView)
    def edit_account(workspace_id: uuid.UUID, account_id: uuid.UUID, body: AccountEdit, actor=Depends(current_actor)):
        return core.accounts.edit(actor, str(workspace_id), str(account_id), **body.model_dump(exclude_none=True))

    @app.delete("/api/v1/workspaces/{workspace_id}/accounts/{account_id}", status_code=204)
    def delete_account(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(current_actor)):
        core.accounts.delete(actor, str(workspace_id), str(account_id))
        return Response(status_code=204)

    @app.post("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/refresh", response_model=dto.AccountView)
    async def refresh(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(current_actor)):
        return await core.accounts.refresh(actor, str(workspace_id), str(account_id))

    @app.get("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/detail", response_model=dto.DetailView)
    async def detail(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(current_actor)):
        return await core.accounts.detail(actor, str(workspace_id), str(account_id))

    @app.get("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/grants", response_model=list[dto.GrantView])
    def grants(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(current_actor)):
        return core.workspaces.grants(actor, str(workspace_id), str(account_id))

    @app.put("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/grants/{user_id}", status_code=204)
    def grant(workspace_id: uuid.UUID, account_id: uuid.UUID, user_id: uuid.UUID, body: GrantChange,
              actor=Depends(current_actor)):
        core.workspaces.set_grant(actor, str(workspace_id), str(account_id), str(user_id), body.level)
        return Response(status_code=204)

    @app.delete("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/grants/{user_id}", status_code=204)
    def revoke_grant(workspace_id: uuid.UUID, account_id: uuid.UUID, user_id: uuid.UUID, actor=Depends(current_actor)):
        core.workspaces.set_grant(actor, str(workspace_id), str(account_id), str(user_id), None)
        return Response(status_code=204)

    @app.get("/api/v1/workspaces/{workspace_id}/audit", response_model=list[dto.AuditView])
    def workspace_audit(workspace_id: uuid.UUID, limit: int = Query(100, ge=1, le=200),
                        offset: int = Query(0, ge=0), actor=Depends(current_actor)):
        return core.workspaces.audits(actor, str(workspace_id), limit=limit, offset=offset)

    @app.get("/api/v1/instance/users", response_model=list[dto.UserView])
    def users(actor=Depends(current_actor)):
        return core.identity.users(actor)

    @app.put("/api/v1/instance/users/{user_id}", status_code=204)
    def user_state(user_id: uuid.UUID, body: UserState, actor=Depends(current_actor)):
        core.identity.set_user_active(actor, str(user_id), body.active)
        return Response(status_code=204)

    @app.get("/api/v1/instance/audit", response_model=list[dto.AuditView])
    def instance_audit(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), actor=Depends(current_actor)):
        return core.workspaces.audits(actor, None, limit=limit, offset=offset)

    @app.get("/api/v1/health", response_model=dto.Health)
    def health():
        with core.db.transaction() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "api_version": 1}

    @app.post("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/manual-switch", response_model=dto.SwitchIssued)
    async def manual_ticket(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(browser_actor)):
        return await core.switches.issue(actor, str(workspace_id), str(account_id))

    @app.post("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/switch-command", response_model=dto.SwitchCommand)
    async def manual_command(workspace_id: uuid.UUID, account_id: uuid.UUID, body: SwitchPlatform,
                             actor=Depends(browser_actor)):
        issued = await core.switches.issue(actor, str(workspace_id), str(account_id))
        url = f"{public_origin}/api/v1/switch/{issued['token']}/{body.platform}"
        return {"platform": body.platform, "expires_at": issued["expires_at"],
                "command": download_command(url, body.platform)}

    @app.get("/api/v1/switch/{token}/{platform}", response_class=PlainTextResponse)
    def manual_download(token: str, platform: Literal["macos", "windows"], request: Request):
        script = core.switches.download(token, request_id=request.state.request_id,
            render=lambda delivery: render_script(delivery, platform, preview=manual_switch_preview)["script"])
        suffix = "sh" if platform == "macos" else "ps1"
        return PlainTextResponse(script, headers={
            "Content-Disposition": f'attachment; filename="switch-account.{suffix}"'})

    @app.post("/api/v1/manual-switch/consume", response_model=dto.ManualScript)
    def manual_consume(body: ManualConsume, actor=Depends(browser_actor)):
        return core.switches.consume(actor, body.token.get_secret_value(),
            render=lambda delivery: render_script(delivery, body.platform, preview=manual_switch_preview))

    @app.post("/api/v1/workspaces/{workspace_id}/accounts/{account_id}/device-switch", response_model=dto.SwitchIssued)
    async def device_ticket(workspace_id: uuid.UUID, account_id: uuid.UUID, actor=Depends(switch_device)):
        return await core.switches.issue(actor, str(workspace_id), str(account_id))

    @app.post("/api/v1/device-switch/consume", response_model=dto.DeviceDelivery)
    def device_consume(body: DeviceConsume, actor=Depends(switch_device)):
        def render(delivery):
            return dto.DeviceDelivery(ticket_id=delivery.ticket_id, workspace_id=delivery.workspace_id,
                account_id=delivery.account_id, expires_at=delivery.expires_at,
                access_token=delivery.secrets.access_token, refresh_token=delivery.secrets.refresh_token,
                email=delivery.email, subject=delivery.subject)
        return core.switches.consume(actor, body.token.get_secret_value(), render=render)

    @app.post("/api/v1/device-switch/{ticket_id}/result", status_code=204)
    def device_result(ticket_id: uuid.UUID, body: DeviceResult, actor=Depends(switch_device)):
        core.switches.record_result(actor, str(ticket_id), body.result)
        return Response(status_code=204)

    if _local is not None:
        allowed = {"bootstrap", "me", "accounts", "account", "authorize_account", "reauthorize_account",
                   "edit_account", "delete_account", "refresh", "detail", "workspace_audit", "health"}
        app.router.routes[:] = [route for route in app.router.routes if route.name in allowed]
        return app

    # Only explicit UI routes are mounted. Unknown API routes never become HTML.
    root = Path(web_dir) if web_dir else Path(__file__).parents[1] / "web_v2"
    if (root / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def frontend():
            return FileResponse(root / "index.html", media_type="text/html")

    return app
