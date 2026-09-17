from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi import HTTPException, Request

from cursor_dashboard import desktop, server, sessions
from cursor_dashboard.client import AuthExpired, CursorClient, RateLimited
from cursor_dashboard.infrastructure.providers.cursor.authorization import verify_identity
from cursor_dashboard.switch_links import download_command


def cookie_for(**claims):
    payload = {"sub": "auth0|user_test", "exp": int(time.time()) + 3600, "type": "web", **claims}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"user_test%3A%3AeyJhbGciOiJIUzI1NiJ9.{encoded}.fake_signature"


def session_response(**claims):
    token = cookie_for(type="session", **claims).partition('%3A%3A')[2]
    return {"accessToken": token, "refreshToken": "distinct-desktop-refresh-token"}


def valid_desktop_session():
    return desktop.desktop_session(session_response(), "auth0|user_test")


class SessionTest(unittest.TestCase):
    def test_login_challenge_uses_pkce_s256(self):
        flow, verifier, challenge = desktop.login_challenge()
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='))
        self.assertNotEqual(flow, desktop.login_challenge()[0])

    def test_desktop_response_must_be_session_for_selected_user(self):
        web = desktop.parse_session(cookie_for()).token
        for data in ({}, {"accessToken": web, "refreshToken": web},
                     session_response(sub='auth0|user_other'),
                     {**session_response(), "refreshToken": ""}):
            with self.subTest(data_keys=list(data)), self.assertRaises(desktop.DesktopSessionError):
                desktop.desktop_session(data, "auth0|user_test")

    def test_accepts_plain_and_encoded_values(self):
        for subject in ("user_test", "auth0|user_test", "google-oauth2|user_test", "github|user_test"):
            cookie = cookie_for(sub=subject)
            for value in (cookie, cookie.replace("%3A%3A", "::"), "WorkosCursorSessionToken=" + cookie):
                with self.subTest(subject=subject, value=value[:20]):
                    self.assertEqual(desktop.parse_session(value).subject, subject)

    def test_rejects_mismatched_or_malformed_oauth_subjects(self):
        for subject in ("google-oauth2|user_other", "github|user_other", "|user_test",
                        "google-oauth2||user_test", "auth0|google-oauth2|user_test",
                        "google oauth2|user_test", "google-oauth2|user_test\n", None, 42, [], {}):
            with self.subTest(subject=subject), self.assertRaises(desktop.DesktopSessionError):
                desktop.parse_session(cookie_for(sub=subject))

    def test_oauth_identity_checks_preserve_provider_prefix(self):
        subject = "google-oauth2|user_test"
        self.assertEqual(desktop.desktop_session(session_response(sub=subject), subject).subject, subject)
        self.assertEqual(verify_identity({"email": "test@example.test", "sub": subject}, subject=subject),
                         "test@example.test")
        for other in ("google-oauth2|user_other", "github|user_test", "auth0|user_test"):
            with self.subTest(other=other):
                with self.assertRaises(desktop.DesktopSessionError):
                    desktop.desktop_session(session_response(sub=other), subject)
                with self.assertRaises(desktop.DesktopSessionError):
                    verify_identity({"email": "test@example.test", "sub": other}, subject=subject)
        with self.assertRaises(desktop.DesktopSessionError):
            desktop.desktop_session(session_response(sub="user_test"), subject)

    def test_bare_profile_identity_matches_oauth_token_subject(self):
        for provider in ("auth0", "google-oauth2", "github"):
            for field in ("sub", "authId"):
                with self.subTest(provider=provider, field=field):
                    self.assertEqual(verify_identity({"email": "test@example.test", field: "user_test"},
                                                     subject=f"{provider}|user_test"), "test@example.test")

    def test_profile_identity_compatibility_rejects_other_accounts_and_providers(self):
        subject = "google-oauth2|user_test"
        for actual in ("user_other", "|user_test", "google-oauth2||user_test", "user_test\n",
                       "github|user_test", "auth0|user_test", None, 42, [], {}):
            with self.subTest(actual=actual), self.assertRaises(desktop.DesktopSessionError):
                verify_identity({"email": "test@example.test", "sub": actual}, subject=subject)
        for identity in ({"email": "other@example.test", "sub": "user_test"},
                         {"email": "test@example.test", "authId": "github|user_test", "sub": "user_test"}):
            with self.assertRaises(desktop.DesktopSessionError):
                verify_identity(identity, email="test@example.test", subject=subject)

    def test_rejects_expired_missing_or_invalid_expiry(self):
        for expiry in (0, time.time() - 10, None, True, "2099", float("nan"), float("inf"), 10**400):
            with self.subTest(expiry=expiry), self.assertRaises(desktop.DesktopSessionError):
                desktop.parse_session(cookie_for(exp=expiry))

    def test_rejects_mismatched_account_and_malformed_tokens(self):
        for cookie in (cookie_for(sub="auth0|user_other"), "opaque-cookie", "user_test::a.e30.c",
                       "user_test::a.not-json.c", cookie_for() + "'; touch /tmp/unexpected"):
            with self.subTest(cookie=cookie[:20]), self.assertRaises(desktop.DesktopSessionError):
                desktop.parse_session(cookie)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.session = valid_desktop_session()
        self.result = desktop.build_commands(self.session, "test@example.test")

    def test_commands_decode_to_inspectable_scripts(self):
        for platform, value in self.result["commands"].items():
            encoded = re.search(r"'([A-Za-z0-9+/=]{100,})'", value["command"]).group(1)
            self.assertEqual(base64.b64decode(encoded).decode(), value["script"])
            self.assertNotIn("__ENGINE__", value["script"])
            self.assertNotIn("__SESSION_JSON__", value["script"])
            self.assertNotIn("__EXPIRES_AT__", value["script"])
            if platform == "macos":
                if Path("/bin/bash").is_file():
                    result = subprocess.run(["/bin/bash", "-n"], input=value["script"], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_account_text_cannot_escape_the_script_here_document(self):
        email = "bad'\nCURSOR_PANEL_JS\n'@\n$(touch /tmp/unexpected)"
        result = desktop.build_commands(self.session, email)
        for item in result["commands"].values():
            self.assertNotIn(email, item["script"])
            encoded_json = item["script"].split("const session = ", 1)[1].split(";\n", 1)[0]
            self.assertEqual(json.loads(encoded_json)["email"], email)

    def test_preview_commands_exit_before_accessing_cursor(self):
        commands = desktop.build_commands(self.session, "preview@example.test", preview=True)["commands"]
        self.assertTrue(commands["macos"]["script"].startswith("exit 1"))
        self.assertTrue(commands["windows"]["script"].startswith("throw '仅供预览，不能执行切换'"))

    def test_web_cookie_can_never_be_written_as_desktop_credentials(self):
        web = desktop.parse_session(cookie_for())
        with self.assertRaises(desktop.DesktopSessionError):
            desktop.build_commands(web, 'test@example.test')


@unittest.skipUnless(Path('/bin/bash').exists(), 'Bash is required for the macOS script tests')
class MacQuitTest(unittest.TestCase):
    """Run the generated shell script with isolated app/process stand-ins."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        app = self.root / 'Cursor.app'
        self.write_executable(app / 'Contents/MacOS/Cursor', '''
import os, sys
from pathlib import Path
root = Path(os.environ['QUIT_TEST_ROOT'])
if sys.argv[1] == '-e' and os.environ['QUIT_TEST_MODE'] == 'runtime-fails':
    print('模拟 SQLite 加载失败', file=sys.stderr)
    sys.exit(2)
if sys.argv[1] == '-':
    sys.stdin.read()
    if os.environ['QUIT_TEST_MODE'] == 'write-fails':
        print('模拟数据库写入失败', file=sys.stderr)
        sys.exit(2)
    (root / 'account-write').touch()
''')
        cli = app / 'Contents/Resources/app/bin/cursor'
        cli.parent.mkdir(parents=True)
        cli.write_text('touch "$QUIT_TEST_ROOT/reopened"\n')
        db = self.root / 'Library/Application Support/Cursor/User/globalStorage/state.vscdb'
        db.parent.mkdir(parents=True)
        db.touch()
        self.running = self.root / 'running'
        self.running.touch()
        self.write_executable(self.bin / 'pgrep', '''
import os, sys
from pathlib import Path
sys.exit(0 if (Path(os.environ['QUIT_TEST_ROOT']) / 'running').exists() else 1)
''')
        self.write_executable(self.bin / 'sleep', 'import time\ntime.sleep(0.01)\n')
        self.write_executable(self.bin / 'osascript', '''
import os, sys, time
from pathlib import Path
root = Path(os.environ['QUIT_TEST_ROOT'])
(root / 'helper-pid').write_text(str(os.getpid()))
mode = os.environ['QUIT_TEST_MODE']
if mode == 'denied':
    sys.exit(1)
if mode in ('success', 'closed-but-helper-hangs'):
    (root / 'running').unlink()
if mode == 'success':
    sys.exit(0)
while True:
    time.sleep(1)
''')
        self.addCleanup(self.stop_helper)
        self.script = desktop.build_commands(valid_desktop_session(), 'test@example.test')['commands']['macos']['script']
        self.script = self.script.replace("CURSOR_APP='/Applications/Cursor.app'", f"CURSOR_APP='{app}'")
        self.script = self.script.replace('CURSOR_DB="$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb"',
                                          f"CURSOR_DB='{db}'")

    def write_executable(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!{sys.executable}\n' + source)
        path.chmod(0o700)

    def stop_helper(self):
        pid_file = self.root / 'helper-pid'
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass

    def run_script(self, mode):
        result = subprocess.run(['/bin/bash'], input=self.script, text=True, capture_output=True,
                                timeout=10, env={**os.environ,
                                'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                                'QUIT_TEST_ROOT': str(self.root), 'QUIT_TEST_MODE': mode})
        pid_file = self.root / 'helper-pid'
        if pid_file.exists():
            with self.assertRaises(ProcessLookupError, msg='AppleScript helper must be reaped'):
                os.kill(int(pid_file.read_text()), 0)
        return result

    def test_blocked_quit_request_times_out_without_writing_or_force_quitting(self):
        result = self.run_script('hang')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('等待退出超过 30 秒', result.stderr)
        self.assertIn('✗ [3/5]', result.stderr)
        self.assertNotIn('\x1b', result.stderr)
        self.assertTrue(self.running.exists())
        self.assertFalse((self.root / 'account-write').exists())
        self.assertFalse((self.root / 'reopened').exists())

    def test_denied_quit_request_stops_without_writing(self):
        result = self.run_script('denied')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('退出请求失败或已取消', result.stderr)
        self.assertTrue(self.running.exists())
        self.assertFalse((self.root / 'account-write').exists())
        self.assertFalse((self.root / 'reopened').exists())

    def test_successful_quit_updates_account_and_reopens(self):
        for mode in ('success', 'closed-but-helper-hangs'):
            with self.subTest(mode=mode):
                self.running.touch()
                for marker in ('account-write', 'reopened'):
                    (self.root / marker).unlink(missing_ok=True)
                result = self.run_script(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.root / 'account-write').exists())
                self.assertTrue((self.root / 'reopened').exists())
                self.assertIn('✓ [5/5]', result.stderr)
                self.assertNotIn('\x1b', result.stderr)

    def test_already_closed_cursor_skips_quit_request(self):
        self.running.unlink()
        result = self.run_script('hang')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'helper-pid').exists())
        self.assertTrue((self.root / 'account-write').exists())
        self.assertTrue((self.root / 'reopened').exists())

    @unittest.skipUnless(os.name == 'posix' and shutil.which('zsh') and shutil.which('curl'),
                         'A POSIX terminal, zsh and curl are required')
    def test_downloaded_script_with_terminal_animation_reopens_exactly_once(self):
        import pty

        # Exercise the copied command, system Bash and a real TTY. Captured stderr
        # disables the spinner and misses Bash 3.2's seekable-stdin offset bug.
        source = self.root / 'switch.sh'
        source.write_text(self.script)
        command = download_command(source.as_uri(), 'macos')
        master, slave = pty.openpty()
        chunks = []

        def drain():
            while True:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                except OSError:
                    break

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        process = subprocess.Popen([shutil.which('zsh'), '-c', command],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=slave,
            start_new_session=True, env={**os.environ, 'TERM': 'xterm',
                'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                'QUIT_TEST_ROOT': str(self.root), 'QUIT_TEST_MODE': 'success'})
        os.close(slave)
        try:
            process.wait(timeout=15)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            # The command has already exited. Some macOS runners retain the
            # process-group record briefly but reject a second cleanup signal.
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
            reader.join(timeout=2)
            os.close(master)
        output = b''.join(chunks).decode()
        self.assertEqual(process.returncode, 0, output)
        self.assertIn('\x1b[2K', output, 'The progress animation must be active')
        for step in range(1, 6):
            self.assertEqual(output.count(f'✓ [{step}/5]'), 1, output)
        self.assertTrue((self.root / 'account-write').exists())
        self.assertTrue((self.root / 'reopened').exists())
        self.assertIn('切换步骤已完成', output)

    def test_native_failure_reports_current_step_and_preserves_error(self):
        self.running.unlink()
        for mode, step, message in (('runtime-fails', 2, '模拟 SQLite 加载失败'),
                                    ('write-fails', 4, '模拟数据库写入失败')):
            with self.subTest(mode=mode):
                result = self.run_script(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'✗ [{step}/5]', result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn(f'✓ [{step}/5]', result.stderr)
                self.assertNotIn('切换步骤已完成', result.stderr)
                self.assertFalse((self.root / 'reopened').exists())


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell is required')
class PowerShellDiscoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.install = self.root / '非默认安装 Cursor'
        self.exe = self.install / 'Cursor.exe'
        manifest = self.install / 'resources/app/package.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
        self.exe.touch()
        self.stale = self.root / 'stale/Cursor.exe'
        self.stale.parent.mkdir()
        self.stale.touch()

    def run_discovery(self, setup='', running='@()', real_source=''):
        helpers = desktop.SCRIPTS.joinpath('switch-windows.ps1').read_text(encoding='utf-8').split('\ntry {\n', 1)[0]
        script = helpers + f"\n$env:CURSOR_EXE = $null\n$env:LOCALAPPDATA = '{self.root}/missing'\n"
        script += f"$env:ProgramFiles = '{self.root}/missing'\n${{env:ProgramFiles(x86)}} = '{self.root}/missing'\n"
        for source in ('Path', 'Registry', 'Shortcut'):
            if source != real_source:
                script += f'function Get-Cursor{source}Candidates {{}}\n'
        script += setup + f'''
try {{
    $found = Find-CursorExecutable -Running {running}
    Write-Host "FOUND:$found"
}} catch {{
    Write-Host $_.Exception.Message
    exit 1
}}
'''
        encoded = base64.b64encode(script.encode()).decode()
        command = ("& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString("
                   f"[Convert]::FromBase64String('{encoded}'))))")
        return subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-Command', command],
                              text=True, encoding='utf-8', capture_output=True, timeout=20)

    def assert_found(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = next((line.removeprefix('FOUND:') for line in result.stdout.splitlines()
                      if line.startswith('FOUND:')), None)
        self.assertIsNotNone(found, result.stdout)
        # Windows temp paths may use an 8.3 alias that PowerShell expands.
        self.assertEqual(Path(found).resolve(), self.exe.resolve())

    def test_running_custom_install_is_preferred(self):
        self.assert_found(self.run_discovery(running=f"@([pscustomobject]@{{ Path = '{self.exe}' }})"))

    def test_each_fallback_skips_stale_candidates(self):
        for source in ('Path', 'Registry', 'Shortcut'):
            with self.subTest(source=source):
                self.assert_found(self.run_discovery(
                    f"function Get-Cursor{source}Candidates {{ '{self.stale}'; '{self.exe}' }}"))

    def test_manual_executable_or_directory_takes_priority(self):
        for path in (f'"{self.exe}"', str(self.install)):
            with self.subTest(path=path):
                self.assert_found(self.run_discovery(f"$env:CURSOR_EXE = '{path}'"))
        result = self.run_discovery(f"$env:CURSOR_EXE = '{self.stale}'",
                                    running=f"@([pscustomobject]@{{ Path = '{self.exe}' }})")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CURSOR_EXE 指定的路径无效', result.stdout)

    def test_cli_path_resolves_editor_instead_of_executing_wrapper(self):
        cli = self.install / 'resources/app/bin/cursor.cmd'
        cli.parent.mkdir()
        cli.touch()
        self.assert_found(self.run_discovery(
            f"function Get-Command {{ [pscustomobject]@{{ Source = '{cli}' }} }}", real_source='Path'))

    def test_registry_icon_path_accepts_quotes_and_index(self):
        self.assert_found(self.run_discovery(f'''
function Get-Item {{
    param($LiteralPath)
    if ($LiteralPath -like 'HK*') {{ throw 'No App Paths entry' }}
    Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath
}}
function Get-ItemProperty {{
    [pscustomobject]@{{ DisplayName = 'Unrelated'; DisplayIcon = '{self.stale}' }}
    [pscustomobject]@{{ DisplayName = 'Cursor (User)'; DisplayIcon = '"{self.exe}",0' }}
}}
''', real_source='Registry'))

    def test_missing_install_explains_recovery_without_claiming_success(self):
        result = self.run_discovery()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('快捷方式', result.stdout)
        self.assertIn('$env:CURSOR_EXE', result.stdout)
        self.assertNotIn('FOUND:', result.stdout)


@unittest.skipUnless(shutil.which('pwsh') and shutil.which('node'), 'PowerShell and Node are required')
class PowerShellProgressTest(unittest.TestCase):
    def run_runtime(self, source):
        helpers = desktop.SCRIPTS.joinpath('switch-windows.ps1').read_text(encoding='utf-8').split('\ntry {\n', 1)[0]
        node = shutil.which('node').replace("'", "''")
        source_literal = source.replace("'", "''")
        script = helpers + f'''
$cursorExe = '{node}'
try {{
    Start-SwitchStep 4 '备份本地数据并写入账号'
    Invoke-CursorRuntime -RuntimeArguments @('-', '中文 空格目录') -Source '{source_literal}'
    Complete-SwitchStep
}} catch {{
    Write-Host $_.Exception.Message
    exit 1
}}
'''
        # Exercise the same nested scriptblock scope as the copied command.
        encoded = base64.b64encode(script.encode()).decode()
        command = ("& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString("
                   f"[Convert]::FromBase64String('{encoded}'))))")
        # Preserve the copied command's nested scope without exceeding Windows'
        # command-line length limit in the deliberately large pipe-buffer test.
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / 'runtime.ps1'
            script_path.write_text(command, encoding='utf-8')
            return subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-File', str(script_path)],
                                  text=True, encoding='utf-8', capture_output=True, timeout=20)

    def test_utf8_input_and_arguments_survive_native_runtime(self):
        # Exceed pipe buffers in both directions to catch synchronous-I/O deadlocks.
        source = "// " + '中文' * 12000 + "\n" + '''
process.stdout.write('输出'.repeat(12000));
process.stderr.write('错误详情'.repeat(12000));
console.log(process.argv[2]);
'''
        result = self.run_runtime(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('输出' * 12000, result.stdout)
        self.assertIn('错误详情' * 12000, result.stdout)
        self.assertIn('中文 空格目录', result.stdout)
        self.assertIn('✓ [4/5]', result.stdout)
        self.assertNotIn('\x1b', result.stdout)

    def test_native_failure_is_not_marked_complete(self):
        result = self.run_runtime("console.error('模拟写入失败'); process.exit(7);")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('模拟写入失败', result.stdout)
        self.assertNotIn('✓ [4/5]', result.stdout)


class DesktopClientTest(unittest.TestCase):
    def test_callback_accepts_plain_success_without_sending_verifier(self):
        client = CursorClient('web-cookie')
        self.addCleanup(client.s.close)
        response = requests.Response()
        response.status_code = 200
        response._content = b'OK'
        with patch.object(client.s, 'request', return_value=response) as request:
            client.desktop_callback('flow', 'challenge')
        self.assertEqual(request.call_args.kwargs['json'], {'uuid': 'flow', 'challenge': 'challenge'})
        self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_poll_and_profile_target_desktop_backend(self):
        client = CursorClient('web-cookie')
        self.addCleanup(client.s.close)
        with patch.object(client, '_call', return_value={}) as call:
            client.desktop_poll('flow', 'verifier')
            self.assertEqual(call.call_args.kwargs['base'], 'https://api2.cursor.sh')
            self.assertEqual(call.call_args.kwargs['params'], {'uuid': 'flow', 'verifier': 'verifier'})
            client.desktop_profile('desktop-token')
            self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Bearer desktop-token')


class SwitchEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = Request({"type": "http", "scheme": "http", "server": ("testserver", 80),
                                "path": "/", "headers": [], "router": server.app.router})
        server._switch_links.clear()
        self.addCleanup(server._switch_links.clear)
        session = valid_desktop_session()
        self.account = {"cookie": cookie_for(), "label": "Test", "email": "test@example.test",
                        "access_token": session.token, "refresh_token": session.refresh_token,
                        "auth_subject": session.subject, "token_expires_at": session.expires_at}
        patcher = patch.object(server, "find_account", return_value=self.account)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(server, "take_manual_token", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(server, "require_switch")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(sessions, "ensure_account", return_value=self.account)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_checks_selected_account_and_disables_caching(self):
        with patch.object(server, "fetch_cursor", return_value={"email": self.account["email"], "authId": "auth0|user_test"}) as fetch:
            response = await server.api_switch_command("test@example.test", self.request)
        fetch.assert_awaited_once_with('', 'Test', 'desktop_me', self.account['access_token'])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(set(json.loads(response.body)["commands"]), {"macos", "windows"})

    async def test_pending_desktop_login_is_bounded(self):
        response = requests.Response()
        response.status_code = 404
        pending = requests.HTTPError(response=response)
        responses = [{'email': self.account['email'], 'sub':'auth0|user_test'}, {}] + [pending] * 5
        with patch.object(server, 'fetch_cursor', side_effect=responses) as fetch, \
             patch.object(server.asyncio, 'sleep'), self.assertRaises(desktop.DesktopSessionError):
            await sessions.exchange_cookie(self.account['cookie'], 'Test', fetch)
        self.assertEqual(fetch.await_count, 7)

    async def test_rejected_or_unknown_sessions_never_generate_commands(self):
        for error, code in ((AuthExpired("expired"), 400), (RateLimited("limited"), 503),
                            (requests.Timeout("timeout"), 502)):
            with self.subTest(error=type(error)), \
                 patch.object(server, "fetch_cursor", side_effect=error), \
                 patch.object(server, "build_commands") as build, \
                 self.assertRaises(HTTPException) as raised:
                await server.api_switch_command("test@example.test", self.request)
            self.assertEqual(raised.exception.status_code, code)
            build.assert_not_called()

    async def test_mismatched_or_empty_identity_is_rejected(self):
        for result in ({}, {"email": "other@example.test"}, {"email": self.account["email"], "sub": "user_other"}):
            with patch.object(server, "fetch_cursor", return_value=result), self.assertRaises(HTTPException) as raised:
                await server.api_switch_command("test@example.test", self.request)
            self.assertEqual(raised.exception.status_code, 400)

    async def test_expired_web_cookie_does_not_block_desktop_switch(self):
        self.account["cookie"] = cookie_for(exp=1)
        with patch.object(server, "fetch_cursor", return_value={'email':self.account['email'], 'authId':'auth0|user_test'}) as fetch:
            response = await server.api_switch_command("test@example.test", self.request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(call.args[0] == '' for call in fetch.await_args_list))

    def test_endpoint_uses_panel_authentication(self):
        route = next(route for route in server.app.routes if route.path.endswith("/switch-command"))
        self.assertIn(server.require_token, [dep.call for dep in route.dependant.dependencies])
        with patch.object(server, "PANEL_TOKEN", "expected"), self.assertRaises(HTTPException):
            server.require_token("wrong")


@unittest.skipUnless(shutil.which("node"), "Node is required for the local engine integration test")
class DatabaseEngineTest(unittest.TestCase):
    """Execute the real engine against disposable databases using Node's built-in SQLite."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["node", "-e", "require('node:sqlite')"], capture_output=True)
        if result.returncode:
            raise unittest.SkipTest("Node with built-in SQLite is required (22.13+)")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "state.vscdb"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            db.executemany("INSERT INTO ItemTable VALUES (?, ?)", [
                ("cursorAuth/accessToken", "old-token"), ("cursorAuth/cachedEmail", "old@example.test"),
                ("cursorAuth/stripeMembershipType", "old-plan"), ("editor.untouched", "keep-me"),
            ])
        # Only adapt the asynchronous binding; all backup/transaction SQL is real.
        module = self.root / "node_modules" / "@vscode" / "sqlite3"
        module.mkdir(parents=True)
        module.joinpath("index.js").write_text('''
const { DatabaseSync } = require('node:sqlite');
exports.OPEN_READWRITE = 2;
exports.Database = class {
  constructor(file, mode, callback) {
    try { this.db = new DatabaseSync(file); queueMicrotask(() => callback(null)); }
    catch (error) { queueMicrotask(() => callback(error)); }
  }
  configure() {}
  run(sql, values, callback) {
    try { this.db.prepare(sql).run(...values); callback(null); } catch (error) { callback(error); }
  }
  get(sql, callback) {
    try { callback(null, this.db.prepare(sql).get()); } catch (error) { callback(error); }
  }
  close(callback) { this.db.close(); callback(null); }
};
''', encoding="utf-8")

    def run_engine(self, *, expired=False, fail_backup=False, web_token=False):
        session = valid_desktop_session()
        script = desktop.build_commands(session, "new@example.test")["commands"]["macos"]["script"]
        engine = script.split("<<'CURSOR_PANEL_JS'\n", 1)[1].split("\nCURSOR_PANEL_JS", 1)[0]
        if expired:
            engine = engine.replace('"expiresAt":' + str(session.expires_at), '"expiresAt":1')
        if web_token:
            engine = engine.replace(session.token, desktop.parse_session(cookie_for()).token)
        if fail_backup:
            engine = "require('fs').writeFileSync = () => { throw new Error('disk full'); };\n" + engine
        return subprocess.run(["node", "-", str(self.root), str(self.database)], input=engine,
                              text=True, capture_output=True, timeout=15)

    def rows(self, filename=None):
        with closing(sqlite3.connect(filename or self.database)) as db, db:
            return dict(db.execute("SELECT key, value FROM ItemTable"))

    def test_switch_preserves_other_settings_and_backs_up_old_account(self):
        before = self.rows()
        result = self.run_engine()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.rows()
        self.assertEqual(after["editor.untouched"], "keep-me")
        self.assertEqual(after["cursorAuth/cachedEmail"], "new@example.test")
        self.assertNotEqual(after["cursorAuth/accessToken"], "old-token")
        self.assertEqual(after['cursorAuth/refreshToken'], 'distinct-desktop-refresh-token')
        self.assertNotIn("cursorAuth/stripeMembershipType", after)
        backup, = self.root.glob("*.bak")
        self.assertEqual(self.rows(backup), before)
        if os.name != "nt":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_write_failure_rolls_back_every_login_field(self):
        before = self.rows()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""CREATE TRIGGER reject_email BEFORE INSERT ON ItemTable
                WHEN NEW.key = 'cursorAuth/cachedEmail'
                BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        result = self.run_engine()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.rows(), before)

    def test_expired_command_does_not_write_or_create_backup(self):
        before = self.rows()
        self.assertNotEqual(self.run_engine(expired=True).returncode, 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(list(self.root.glob("*.bak")), [])

    def test_backup_failure_leaves_original_account_unchanged(self):
        before = self.rows()
        self.assertNotEqual(self.run_engine(fail_backup=True).returncode, 0)
        self.assertEqual(self.rows(), before)

    def test_web_token_is_rejected_before_opening_database(self):
        before = self.rows()
        result = self.run_engine(web_token=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('缺少已验证的桌面登录凭证', result.stderr)
        self.assertEqual(self.rows(), before)
        self.assertEqual(list(self.root.glob('*.bak')), [])

    def test_backup_includes_committed_wal_contents(self):
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute('PRAGMA journal_mode = WAL')
            writer.execute('INSERT INTO ItemTable VALUES (?, ?)', ('editor.wal', 'latest'))
            writer.commit()
            self.assertTrue(Path(str(self.database) + '-wal').exists())
            result = self.run_engine()
            self.assertEqual(result.returncode, 0, result.stderr)
            backup, = self.root.glob('*.bak')
            self.assertEqual(self.rows(backup)['editor.wal'], 'latest')


if __name__ == "__main__":
    unittest.main()
