"""Loopback-only P3 preview: disposable database, synthetic identities, no provider network."""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import sys
import threading
import time

import uvicorn

from cursor_dashboard.api.app import create_app
from cursor_dashboard.desktop import parse_session, token_claims
from cursor_dashboard.domain.core import Actor, Secrets
from cursor_dashboard.infrastructure.secrets import FileKeyProvider
from cursor_dashboard.runtime.core import Core
from cursor_dashboard.runtime.settings import CoreConfig
from cursor_dashboard.usage import assemble_desktop

PASSWORD = 'Preview password 42!'


def token(subject='user_preview'):
    body = base64.urlsafe_b64encode(json.dumps({'sub': subject if '|' in subject else 'auth0|' + subject, 'type': 'session',
        'exp': int(time.time()) + 7 * 86400}).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJIUzI1NiJ9.' + body + '.synthetic'


class PreviewGateway:
    def __init__(self):
        self.flows = {}

    async def __call__(self, cookie, label, name, *args):
        if name == 'me':
            subject = parse_session(cookie).subject.removeprefix('auth0|')
        elif name == 'desktop_callback':
            self.flows[args[0]] = parse_session(cookie).subject.removeprefix('auth0|')
            return {}
        elif name == 'desktop_poll':
            subject = self.flows.pop(args[0])
            return {'accessToken': token(subject), 'refreshToken': 'preview-' + subject}
        elif name == 'desktop_refresh':
            return {'access_token': token(args[0].removeprefix('preview-')), 'refresh_token': args[0]}
        else:
            subject = token_claims(args[0])['sub'].removeprefix('auth0|')
        if name in {'me', 'desktop_me'}:
            identity = subject if '|' in subject else 'auth0|' + subject
            user_id = subject.split('|', 1)[-1]
            email = user_id.removeprefix('user_') + '@example.test'
            # The web profile returns a bare sub, while issued tokens retain the provider.
            return {'email': email, 'sub': user_id} if name == 'me' else {
                'email': email, 'authId': identity}
        if name == 'desktop_plan':
            return {'planInfo': {'planName': 'Pro', 'includedAmountCents': 2000}}
        if name == 'desktop_profile':
            return {'membershipType': 'pro'}
        if name == 'desktop_period':
            start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = (start + timedelta(days=32)).replace(day=1)
            exhausted = subject == 'user_quota_capped'
            return {'billingCycleStart': int(start.timestamp() * 1000),
                    'billingCycleEnd': int(end.timestamp() * 1000),
                    'planUsage': {'autoPercentUsed': 100 if exhausted else 28, 'apiPercentUsed': 100 if exhausted else 36,
                    'totalPercentUsed': 100 if exhausted else 32, 'totalSpend': 49615 if exhausted else 1600,
                    'includedSpend': 2000 if exhausted else 1600}}
        if name == 'desktop_grok':
            now = datetime.now(timezone.utc)
            return {'usagePercent': 18, 'hasNonZeroIncludedLimit': True,
                    'currentPeriodStart': now.isoformat(), 'nextResetTimestampUtc': (now + timedelta(days=7)).isoformat()}
        if name == 'desktop_limit':
            return {'noUsageBasedAllowed': True}
        if name == 'desktop_aggregated':
            await asyncio.sleep(.1)
            return {'aggregations': [
                {'modelIntent': 'Composer', 'tier': 2, 'totalCents': 550, 'inputTokens': 190000, 'outputTokens': 13000},
                {'modelIntent': 'Claude Sonnet', 'tier': 1, 'totalCents': 890, 'inputTokens': 285000, 'outputTokens': 18000},
                {'modelIntent': 'Grok', 'tier': 2, 'totalCents': 160, 'inputTokens': 64000, 'outputTokens': 4200},
            ]}
        raise RuntimeError('Unknown preview provider operation')


async def seed(core):
    first = core.identity.initialize_server('owner@example.test', PASSWORD)
    actor = Actor(first['user_id'])
    team = core.workspaces.create(actor, 'Studio 开发组')
    invitation = core.workspaces.invite(actor, team['id'], 'member@example.test', 'member')
    member = core.workspaces.accept(invitation['token'], password=PASSWORD)
    invite = core.workspaces.invite(actor, team['id'], 'viewer@example.test', 'viewer')
    core.workspaces.accept(invite['token'], password=PASSWORD)
    gateway = core.accounts.gateway
    for workspace, labels in ((first['workspace_id'], ['日常开发', '备用账号']), (team['id'], ['团队主账号', '设计协作', '实验环境'])):
        for index, label in enumerate(labels):
            subject = 'user_preview' + str(index)
            snapshot = assemble_desktop(label, {'email': 'preview' + str(index) + '@example.test', 'authId': subject},
                await gateway('', label, 'desktop_plan', token(subject)), await gateway('', label, 'desktop_profile', token(subject)),
                await gateway('', label, 'desktop_period', token(subject)), await gateway('', label, 'desktop_grok', token(subject)),
                await gateway('', label, 'desktop_limit', token(subject)))
            saved = core.repository.put_authorization(actor, workspace, email='preview' + str(index) + '@example.test',
                subject=subject, label=label, secrets=Secrets('preview-cookie', token(subject), 'preview-' + subject),
                expires_at=int(time.time()) + 7 * 86400, data=snapshot, tags=[['开发', '备用', '实验'][index]])
            if workspace == team['id'] and index == 0:
                core.workspaces.set_grant(actor, workspace, saved.ref.account_id, member['user_id'], 'use')
    return first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18763)
    parser.add_argument('--parent-pipe', action='store_true', help='Exit when the test launcher closes stdin')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='cursor-p3-preview-') as temporary:
        key = Path(temporary) / 'master.json'
        FileKeyProvider.create(key)
        config = CoreConfig(Path(temporary) / 'data', key, mode='server', refresh_margin=0, request_interval=0)
        with Core(config, initialize=True, gateway=PreviewGateway()) as core:
            asyncio.run(seed(core))
            app = create_app(core, public_origin=f'http://127.0.0.1:{args.port}', web_dir=root / 'frontend' / 'dist', manual_switch_preview=True)
            print(f'Synthetic preview: http://127.0.0.1:{args.port} · owner/member/viewer@example.test · password: {PASSWORD}', flush=True)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, access_log=False, proxy_headers=False))
            if args.parent_pipe:
                def parent_lifetime():
                    sys.stdin.buffer.read()
                    server.should_exit = True
                threading.Thread(target=parent_lifetime, daemon=True).start()
            server.run()


if __name__ == '__main__':
    main()
