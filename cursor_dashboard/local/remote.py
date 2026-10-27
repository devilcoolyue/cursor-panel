"""Configured instances and native-only device sessions. No credential IPC output."""
from __future__ import annotations

from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import webbrowser

import requests

from ..api.app import validate_origin
from ..application.devices import challenge
from ..application.switching import SwitchDelivery
from ..domain.core import Conflict, CoreError, Secrets, SecretError
from .files import write_new
from .keys import SystemKeyStore


class RemoteError(CoreError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class DeviceStore(SystemKeyStore):
    service = "dev.cursor-panel.desktop.devices"

    def read_session(self, connection_id):
        try:
            value = self.backend().get_password(self.service, self.account + ":" + connection_id)
            return json.loads(value) if value else None
        except Exception:
            raise SecretError("System device credentials are locked or unavailable") from None

    def save_session(self, connection_id, value):
        try:
            self.backend().set_password(self.service, self.account + ":" + connection_id, json.dumps(value))
            if self.read_session(connection_id) != value:
                raise ValueError()
        except Exception:
            raise SecretError("Cannot persist the device session") from None

    def delete_session(self, connection_id):
        try:
            key = self.account + ":" + connection_id
            if self.backend().get_password(self.service, key) is not None:
                self.backend().delete_password(self.service, key)
        except Exception:
            raise SecretError("Cannot remove the device session from the system store") from None


def remote_http(origin, method, path, *, token=None, body=None):
    # Origin is user-configured, never taken from an upstream redirect or UI
    # request path. Cookies, environment proxies and redirects are not followed.
    validate_origin(origin)
    if not path.startswith("/api/v1/") or "#" in path:
        raise Conflict("Invalid remote operation")
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.request(method, origin + path, json=body,
                    headers={"Authorization": "Bearer " + token} if token else {},
                    timeout=(5, 30), allow_redirects=False, stream=True) as response:
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    raise RemoteError(status if status in {401, 403, 404, 409, 422, 429, 502, 503} else 502,
                                      "Remote request failed; reconnect or retry")
                if response.status_code == 204:
                    return None
                payload = bytearray()
                deadline = time.monotonic() + 60
                for chunk in response.iter_content(65536):
                    payload.extend(chunk)
                    if len(payload) > 4 * 1024 * 1024 or time.monotonic() > deadline:
                        raise RemoteError(502, "Remote response is too large or too slow")
                return json.loads(payload)
    except (requests.RequestException, ValueError):
        raise RemoteError(502, "Remote instance is unavailable or returned an invalid response") from None


_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SPACE = rf"/workspaces/{_ID}"
_ACCOUNT = rf"{_SPACE}/accounts/{_ID}"
# A second allowlist protects the private Python adapter as well as the Rust
# enum. In particular, no auth exchange, manual scripts or credential delivery.
_PUBLIC_ROUTES = {
    "GET": [r"/bootstrap", r"/me", r"/auth/sessions", r"/auth/devices", r"/instance/(users|audit)",
            rf"{_SPACE}/(accounts|members|invitations|audit)", _ACCOUNT, rf"{_ACCOUNT}/(detail|grants)"],
    "POST": [r"/workspaces", rf"{_SPACE}/(authorizations|invitations)", rf"{_SPACE}/authorizations/{_ID}/poll",
             rf"{_ACCOUNT}/refresh"],
    "PUT": [r"/auth/password", rf"/instance/users/{_ID}", rf"{_SPACE}/owner",
            rf"{_SPACE}/members/{_ID}", rf"{_ACCOUNT}/grants/{_ID}"],
    "PATCH": [_ACCOUNT],
    "DELETE": [rf"/auth/(sessions|devices)/{_ID}", _SPACE, _ACCOUNT, rf"{_SPACE}/authorizations/{_ID}",
               rf"{_SPACE}/(members|invitations)/{_ID}", rf"{_ACCOUNT}/grants/{_ID}"],
}


def public_route(method, path):
    parsed = urlsplit(path)
    if (parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/api/v1/")
            or not any(re.fullmatch(pattern, parsed.path[7:]) for pattern in _PUBLIC_ROUTES.get(method, []))):
        raise Conflict("Unsupported remote operation")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"q", "tag", "limit", "offset"} or any(len(v) != 1 for v in query.values()) or len(path) > 4096:
        raise Conflict("Invalid remote query")
    return path


class Connections:
    def __init__(self, directory, *, store=None, transport=None, browser=None):
        self.path = directory / "connections.json"
        self.store = store or DeviceStore(directory)
        self.http = transport or remote_http
        self.browser = browser or webbrowser.open
        self.guard = threading.RLock()
        self.active = None
        self.generation = 0
        self.pending = None
        self.rows = []
        self.states = {}
        if self.path.exists():
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 65536:
                    raise ValueError()
                rows = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(rows, list) or len(rows) > 20:
                    raise ValueError()
                for row in rows:
                    if set(row) != {"id", "name", "origin", "device_id"}:
                        raise ValueError()
                    uuid.UUID(row["id"])
                    uuid.UUID(row["device_id"])
                    validate_origin(row["origin"])
                    if not isinstance(row["name"], str) or not 1 <= len(row["name"]) <= 128:
                        raise ValueError()
                if len({r["id"] for r in rows}) != len(rows):
                    raise ValueError()
                self.rows = rows
            except (OSError, ValueError, TypeError, KeyError, CoreError):
                raise Conflict("Instance connection settings are invalid") from None

    def _save(self):
        pending = self.path.with_suffix(".tmp")
        pending.unlink(missing_ok=True)
        write_new(pending, json.dumps(self.rows, ensure_ascii=False).encode("utf-8"))
        pending.replace(self.path)

    def _row(self, connection_id):
        for row in self.rows:
            if row["id"] == connection_id:
                return dict(row)
        raise Conflict("Instance connection is unavailable")

    def snapshot(self):
        with self.guard:
            return {"active_id": self.active, "generation": self.generation,
                "items": [{**row, "phase": self.states.get(row["id"], "saved")} for row in self.rows]}

    def add(self, name, origin):
        origin = origin.strip().rstrip("/")
        validate_origin(origin)
        if not name.strip() or len(name.strip()) > 128:
            raise Conflict("Instance name must contain 1 to 128 characters")
        with self.guard:
            if len(self.rows) >= 20 or any(row["origin"] == origin for row in self.rows):
                raise Conflict("Instance already exists or connection limit reached")
            row = {"id": str(uuid.uuid4()), "name": name.strip(), "origin": origin, "device_id": str(uuid.uuid4())}
            self.rows.append(row)
            try:
                self._save()
            except BaseException:
                self.rows.remove(row)
                raise
            return self.snapshot()

    def select(self, connection_id):
        with self.guard:
            if connection_id is not None:
                self._row(connection_id)
            self._cancel()
            self.active = connection_id
            self.generation += 1
            return self.snapshot()

    def negotiate(self, row):
        info = self.http(row["origin"], "GET", "/api/v1/bootstrap")
        if (not isinstance(info, dict) or type(info.get("api_version")) is not int or info["api_version"] != 1
                or info.get("mode") != "server" or info.get("initialized") is not True
                or not isinstance(info.get("capabilities"), dict)
                or info["capabilities"].get("device_sessions") is not True):
            raise RemoteError(426, "Instance protocol is incompatible; upgrade the server or desktop")
        return info

    def _session(self, row):
        value = self.store.read_session(row["id"])
        if (not isinstance(value, dict) or value.get("origin") != row["origin"]
                or not isinstance(value.get("token"), str) or not isinstance(value.get("expires_at"), (int, float))
                or value["expires_at"] <= time.time()):
            self.states[row["id"]] = "login_required"
            raise RemoteError(401, "Device login expired; sign in with the system browser")
        return value

    def _cancel(self):
        if self.pending:
            self.pending["cancel"].set()
            self.states[self.pending["id"]] = "saved"
            self.pending = None

    def login(self, connection_id):
        with self.guard:
            row = self._row(connection_id)
            epoch = self.generation
        self.negotiate(row)
        with self.guard:
            if epoch != self.generation or self._row(connection_id) != row:
                raise Conflict("Connection changed; start login again")
            self._cancel()
            pending = {"id": connection_id, "cancel": threading.Event(), "state": secrets.token_urlsafe(32),
                       "verifier": secrets.token_urlsafe(32), "deadline": time.monotonic() + 300}
            manager = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Never log the authorization code or state.

                def do_GET(self):
                    parsed = urlsplit(self.path)
                    values = parse_qs(parsed.query, keep_blank_values=True)
                    valid = (len(self.path) <= 1024 and parsed.path == "/callback" and not parsed.netloc
                        and not parsed.fragment and self.headers.get("Host") == urlsplit(pending["callback"]).netloc
                        and self.headers.get("Origin") is None and set(values) == {"code", "state"}
                        and all(len(v) == 1 for v in values.values())
                        and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", values.get("code", [""])[0])
                        and re.fullmatch(r"[A-Za-z0-9_-]{43,128}", values.get("state", [""])[0])
                        and hmac.compare_digest(values.get("state", [""])[0], pending["state"]))
                    if not valid:
                        self.reply(400, "Invalid callback. Return to Cursor Panel and retry.")
                        return
                    try:
                        manager._complete(row, pending, values["code"][0])
                        self.reply(200, "Device connected. You can close this page and return to Cursor Panel.")
                    except Exception:
                        with manager.guard:
                            if manager.pending is pending:
                                manager.states[connection_id] = "login_failed"
                        self.reply(400, "Login failed. Return to Cursor Panel and start again.")
                    finally:
                        pending["cancel"].set()

                def reply(self, status, message):
                    payload = message.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                    self.end_headers()
                    with suppress(OSError):
                        self.wfile.write(payload)

                def setup(self):
                    super().setup()
                    self.connection.settimeout(2)

            listener = HTTPServer(("127.0.0.1", 0), Callback)
            listener.timeout = 0.2
            pending["callback"] = f"http://127.0.0.1:{listener.server_port}/callback"
            self.pending = pending
            self.states[connection_id] = "awaiting_browser"

            def serve():
                try:
                    while not pending["cancel"].is_set() and time.monotonic() < pending["deadline"]:
                        listener.handle_request()
                finally:
                    listener.server_close()
                    with manager.guard:
                        if manager.pending is pending:
                            manager.pending = None
                            if manager.states[connection_id] == "awaiting_browser":
                                manager.states[connection_id] = "login_expired"

            threading.Thread(target=serve, daemon=True, name="device-login-callback").start()
            url = row["origin"] + "/#/device?" + urlencode({"code_challenge": challenge(pending["verifier"]),
                "state": pending["state"], "callback": pending["callback"], "device_id": row["device_id"],
                "device_name": "Cursor Panel Desktop"})
            try:
                if not self.browser(url):
                    raise OSError()
            except Exception:
                self._cancel()
                raise Conflict("Cannot open the system browser") from None
            return self.snapshot()

    def _complete(self, row, pending, code):
        with self.guard:
            if self.pending is not pending or pending["cancel"].is_set() or time.monotonic() >= pending["deadline"]:
                raise Conflict("Login was cancelled or expired")
        issued = self.http(row["origin"], "POST", "/api/v1/auth/devices/exchange", body={
            "code": code, "verifier": pending["verifier"], "callback": pending["callback"], "device_id": row["device_id"]})
        try:
            if (not isinstance(issued, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", issued.get("token", ""))
                    or not isinstance(issued.get("expires_at"), (float, int)) or issued["expires_at"] <= time.time()):
                raise ValueError()
            uuid.UUID(issued["session_id"])
        except (ValueError, TypeError, KeyError):
            raise RemoteError(502, "Invalid device session response") from None
        saved = False
        try:
            with self.guard:
                if self.pending is not pending or pending["cancel"].is_set() or time.monotonic() >= pending["deadline"]:
                    raise Conflict("Login was cancelled or expired")
                self.store.save_session(row["id"], {**issued, "origin": row["origin"]})
                saved = True
                self.states[row["id"]] = "connected"
        finally:
            if not saved:
                with suppress(CoreError):
                    self.http(row["origin"], "DELETE", "/api/v1/auth/devices/" + issued["session_id"], token=issued["token"])

    def disconnect(self, connection_id, *, remove=False):
        with self.guard:
            row = self._row(connection_id)
            if self.pending and self.pending["id"] == connection_id:
                self._cancel()
            self.generation += 1
            value = self.store.read_session(connection_id)
            revoked = True
            if value:
                try:
                    self.http(row["origin"], "DELETE", "/api/v1/auth/devices/" + str(uuid.UUID(value["session_id"])),
                              token=value["token"])
                except RemoteError as error:
                    revoked = error.status == 401
                self.store.delete_session(connection_id)
            self.states[connection_id] = "login_required"
            if remove:
                self.rows = [r for r in self.rows if r["id"] != connection_id]
                self._save()
            if self.active == connection_id:
                self.active = None
            return {**self.snapshot(), "revoked": revoked}

    def request(self, connection_id, method, path, body=None):
        public_route(method, path)
        with self.guard:
            row = self._row(connection_id)
            if self.active != connection_id:
                raise Conflict("Instance connection changed")
            generation = self.generation
        try:
            info = self.negotiate(row)
            if path == "/api/v1/bootstrap":
                result = {**info, "capabilities": {**info["capabilities"], "manual_switch": False,
                           "native_switch": info["capabilities"].get("remote_switch") is True, "archives": False}}
            else:
                with self.guard:
                    if generation != self.generation or self.active != connection_id:
                        raise Conflict("Instance connection changed")
                    value = self._session(row)
                result = self.http(row["origin"], method, path, token=value["token"], body=body)
            with self.guard:
                if generation != self.generation or self.active != connection_id:
                    raise Conflict("Instance connection changed; discard the previous response")
                if path != "/api/v1/bootstrap":
                    self.states[connection_id] = "connected"
            return result
        except RemoteError as error:
            with self.guard:
                if generation == self.generation:
                    self.states[connection_id] = ("login_required" if error.status == 401 else
                        "incompatible" if error.status == 426 else "offline" if error.status in {502, 503} else "connected")
            raise

    def delivery(self, connection_id, workspace_id, account_id):
        # Only the executor calls this path; it never returns through UI IPC.
        with self.guard:
            row = self._row(connection_id)
            if self.active != connection_id:
                raise Conflict("Instance connection changed")
            generation = self.generation
            value = self._session(row)
        if self.negotiate(row)["capabilities"].get("remote_switch") is not True:
            raise RemoteError(403, "Remote switching is not supported by this instance")
        path = f"/api/v1/workspaces/{uuid.UUID(workspace_id)}/accounts/{uuid.UUID(account_id)}/device-switch"
        issued = self.http(row["origin"], "POST", path, token=value["token"])
        with self.guard:
            if self.generation != generation or self.active != connection_id:
                raise Conflict("Instance connection changed; request a new switch")
        data = self.http(row["origin"], "POST", "/api/v1/device-switch/consume", token=value["token"], body={"token": issued["token"]})
        with self.guard:
            if self.generation != generation or self.active != connection_id:
                raise Conflict("Instance connection changed; request a new switch")
        if data["workspace_id"] != workspace_id or data["account_id"] != account_id:
            raise Conflict("Remote switch does not match the selected account")
        delivery = SwitchDelivery(str(uuid.UUID(data["ticket_id"])), account_id, workspace_id, data["expires_at"],
                                 Secrets(access_token=data["access_token"], refresh_token=data["refresh_token"]),
                                 data["email"], data["subject"])
        return delivery, lambda result: self.http(row["origin"], "POST", "/api/v1/device-switch/" + delivery.ticket_id + "/result",
                                                 token=value["token"], body={"result": result})

    def close(self):
        with self.guard:
            self._cancel()
            self.generation += 1
