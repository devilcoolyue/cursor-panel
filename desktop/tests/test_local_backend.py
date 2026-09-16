"""Real private HTTP and packaged OS key-store persistence using owned temporary data."""
from __future__ import annotations
import json
import base64
import faulthandler
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

from cursor_dashboard.local.keys import SystemKeyStore

ROOT = Path(__file__).resolve().parents[1]

class StartupFailureTest(unittest.TestCase):
    def run_failure(self, directory):
        binary = os.environ.get('P4_BACKEND_BINARY')
        command = [binary] if binary else [sys.executable, str(ROOT / 'sidecar/local_backend.py')]
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        token = secrets.token_hex(32)
        try:
            child.stdin.write((json.dumps({'token': token, 'data_dir': str(directory), 'fixture': False}) + '\n').encode())
            child.stdin.flush()
            # Deliberately keep the parent's pipe OPEN. communicate() would
            # hide the daemon BufferedReader shutdown crash this guards against.
            self.assertEqual(child.wait(timeout=20), 1)
            output = child.stdout.read()
            self.assertEqual(child.stderr.read(), b'')
            self.assertNotIn(token.encode(), output)
            self.assertNotIn(b'private-input', output)
            return json.loads(output)
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

    def test_startup_failure_exits_without_secondary_crash_and_reports_safe_reason(self):
        with tempfile.TemporaryDirectory(prefix='cursor-startup-failure-') as temp:
            directory = Path(temp) / 'private-input-not-a-directory'
            directory.write_text('synthetic fixture')
            result = self.run_failure(directory)
            self.assertEqual(result['error']['code'], 'data_io')
            self.assertIsInstance(result['error']['os_error'], int)

    def test_directory_lock_failure_is_reported_before_readiness(self):
        from cursor_dashboard.runtime.lock import RuntimeLock
        with tempfile.TemporaryDirectory(prefix='cursor-startup-lock-') as temp:
            directory = Path(temp)
            with RuntimeLock(directory / '.desktop.lock'):
                self.assertEqual(self.run_failure(directory)['error']['code'], 'data_in_use')

@unittest.skipUnless(sys.platform in {'darwin', 'win32'}, 'Native OS key store requires macOS or Windows')
class LocalBackendTest(unittest.TestCase):
    def setUp(self):
        faulthandler.dump_traceback_later(120)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.temp = tempfile.TemporaryDirectory(prefix='cursor-p4-backend-')
        self.directory = Path(self.temp.name) / 'fixture-data'
        self.http = build_opener(ProxyHandler({}))
        self.addCleanup(self.cleanup)
        self.start()

    def start(self):
        binary = os.environ.get('P4_BACKEND_BINARY')
        command = [binary] if binary else [sys.executable, str(ROOT / 'sidecar/local_backend.py')]
        self.child = subprocess.Popen(command, cwd=self.temp.name, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.token = secrets.token_hex(32)
        self.child.stdin.write(json.dumps({'token': self.token, 'data_dir': str(self.directory), 'fixture': True}) + '\n')
        self.child.stdin.flush()
        messages = queue.Queue()
        threading.Thread(target=lambda: messages.put(self.child.stdout.readline()), daemon=True).start()
        line = messages.get(timeout=60)
        self.assertTrue(line, 'Backend did not provide readiness')
        self.ready = json.loads(line)
        self.base = f'http://127.0.0.1:{self.ready["port"]}'

    def stop(self):
        if self.child.stdin and not self.child.stdin.closed:
            self.child.stdin.close()
        try:
            self.child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=5)
            raise AssertionError('Backend did not exit on parent EOF')
        finally:
            self.child.stdout.close()
            self.child.stderr.close()

    def test_oauth_import_through_packaged_backend(self):
        status, me = self.request('/api/v1/me')
        self.assertEqual(status, 200)
        workspace = me['workspaces'][0]['id']
        subject = 'google-oauth2|user_packaged_oauth'
        claims = {'sub': subject, 'type': 'web', 'exp': int(time.time()) + 3600}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        cookie = f'user_packaged_oauth%3A%3AeyJhbGciOiJIUzI1NiJ9.{payload}.synthetic'
        path = f'/api/v1/workspaces/{workspace}/accounts'
        status, account = self.request(path, 'POST', {'cookie': cookie, 'label': 'Packaged OAuth'})
        self.assertEqual(status, 201, account)
        self.assertEqual(account['email'], 'packaged_oauth@example.test')
        self.assertIsNotNone(account['data'])
        self.assertNotIn(cookie, json.dumps(account))
        status, refreshed = self.request(f"{path}/{account['id']}/authorization", 'POST', {'cookie': cookie})
        self.assertEqual(status, 200, refreshed)
        self.assertNotEqual(account['authorization_generation'], refreshed['authorization_generation'])

    def cleanup(self):
        if self.child.poll() is None:
            self.stop()
        store = SystemKeyStore(self.directory)
        from keyring.errors import PasswordDeleteError
        try:
            store.backend().delete_password(store.service, store.account)
        except PasswordDeleteError:
            pass
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        values = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', **(headers or {})}
        values = {key: value for key, value in values.items() if value is not None}
        try:
            response = self.http.open(Request(self.base + path, method=method,
                data=json.dumps(body).encode() if body is not None else None, headers=values), timeout=15)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            return response.status, json.loads(data) if data else None

    def test_os_key_store_core_snapshot_and_private_boundary(self):
        self.assertEqual(self.request('/native/status')[1]['phase'], 'ready')
        self.assertEqual(self.request('/api/v1/bootstrap', headers={'Authorization': None})[0], 401)
        self.assertEqual(self.request('/api/v1/bootstrap', headers={'Origin': 'http://evil.test'})[0], 403)
        self.assertEqual(self.request('/api/v1/bootstrap', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.request('/api/v1/auth/login', 'POST', {})[0], 404)
        identity = self.request('/api/v1/me')[1]
        workspace = identity['workspaces'][0]['id']
        rows = self.request(f'/api/v1/workspaces/{workspace}/accounts')[1]['items']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['data'] for row in rows))
        self.assertNotIn('fixture-cookie', json.dumps(rows))
        self.assertEqual(self.ready['frozen'], bool(os.environ.get('P4_BACKEND_BINARY')))
        # Persistence is verified through restart and decryption by the same native executable.
        self.stop()
        self.start()
        self.assertEqual(self.request('/api/v1/me')[1]['id'], identity['id'])
        self.assertEqual(self.request(f'/api/v1/workspaces/{workspace}/accounts')[1]['items'], rows)
        self.stop()
        self.assertEqual(self.child.returncode, 0)

    def test_encrypted_archive_and_fixture_switch_over_actual_http(self):
        workspace = self.request('/api/v1/me')[1]['workspaces'][0]['id']
        rows = self.request(f'/api/v1/workspaces/{workspace}/accounts')[1]['items']
        path = self.directory / 'fixture.cursorarchive'
        status, body = self.request('/native/archive/export', 'POST', {'workspace_id': workspace,
            'path': str(path), 'password': 'fixture archive 42'})
        self.assertEqual(status, 200)
        self.assertEqual(body['count'], 2)
        self.assertNotIn(b'fixture-cookie', path.read_bytes())
        status, body = self.request('/native/switch', 'POST', {'workspace_id': workspace,
            'account_id': rows[0]['id'], 'confirmed': True})
        self.assertEqual(status, 200)
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.request('/native/switch')[1]
            if not state['busy']:
                break
            time.sleep(.1)
        self.assertEqual(state['stage'], 'complete')
        self.assertTrue(self.request('/native/backups')[1])
        self.stop()

if __name__ == '__main__':
    unittest.main()
