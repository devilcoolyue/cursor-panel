<div align="center">

<img src="frontend/src/icon.svg" alt="Cursor Panel logo" width="112" height="112" />

# Cursor Panel

**All your Cursor accounts. One clear view.**

Track quotas, switch local accounts, and share access with your team.<br />
Use the standalone desktop app or bring your own server.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Tauri](https://img.shields.io/badge/Tauri-2-24C8D8?logo=tauri&logoColor=white)](https://tauri.app/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](deploy/v2/compose.yaml)
[![Status](https://img.shields.io/badge/Status-V2%20preview-D4A34A)](docs/supported-platforms.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-93DBB5)](LICENSE)

**English** | [简体中文](README_CN.md)

[Download](https://github.com/devilcoolyue/cursor-panel/releases) · [Quick start](#quick-start) · [Documentation](#documentation) · [Report an issue](https://github.com/devilcoolyue/cursor-panel/issues)

</div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/dashboard-dark.png" />
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/dashboard-light.png" />
  <img src="docs/assets/dashboard-light.png" alt="Cursor Panel team workspace with three accounts, remaining quotas, billing cycles, and tag filters" width="1440" />
</picture>

<p align="center"><sub>Current V2 preview · Liquid Glass theme · Synthetic demo accounts · The app interface is currently in Chinese.</sub></p>

## Why Cursor Panel?

- **See every account at a glance.** Compare plans, billing cycles, remaining Cursor / Other Models quotas, and Grok weekly usage. Open an account for model-level usage details.
- **Keep your accounts organized.** Name accounts, add tags, search, and sort. Personal and team workspaces keep different groups of accounts separate.
- **Switch accounts from your desktop.** On macOS and Windows, the local app closes Cursor normally, backs up its login database, updates the account, and restarts it. Restore a backup from settings when needed.
- **Share access deliberately.** Invite teammates, assign roles, and grant `view` or `use` access to individual accounts. Review workspace activity in the audit log.
- **Start locally, connect when needed.** The desktop app runs without a server. Save multiple remote instances and sign in through your system browser with revocable device sessions.
- **Keep credentials protected.** Account credentials are encrypted at rest. Desktop keys live in the system credential store; encrypted archives support account transfer and key recovery.
- **Make it yours.** Six skins, independent light / dark modes, configurable account cards, a collapsible sidebar, and a responsive Web layout. Built-in guidance helps you get started.

This README describes the current V2 preview source. The latest downloadable release is [v0.0.7](https://github.com/devilcoolyue/cursor-panel/releases/tag/v0.0.7); its exact contents are listed in the [release archive](docs/archive/v0.0.7.md). Screenshots use synthetic preview data.

## Choose your setup

| Entry point | Best for | Where accounts live | Cursor switching |
| --- | --- | --- | --- |
| **Standalone desktop** | Managing accounts on your own computer | Local database | Direct local switching on macOS / Windows |
| **Self-hosted Web** | Sharing accounts with a team | Your server | Generates a short-lived terminal command for macOS / Windows |
| **Desktop connected to a server** | Accessing personal and team spaces across instances | The selected server | Remote desktop switching is currently disabled |
| **Remote CLI** | Querying accounts from the terminal | Your server | Query and refresh operations |

Local and remote databases remain independent. Changing a desktop connection does not upload or synchronize local accounts.

## Quick start

### Desktop — for personal use

Download the package for your platform from **[v0.0.7 Releases](https://github.com/devilcoolyue/cursor-panel/releases/tag/v0.0.7)**:

| Platform | Download |
| --- | --- |
| macOS · Apple Silicon | [DMG for arm64](https://github.com/devilcoolyue/cursor-panel/releases/download/v0.0.7/Cursor.Panel_0.0.7_aarch64.dmg) |
| macOS · Intel | [DMG for x64](https://github.com/devilcoolyue/cursor-panel/releases/download/v0.0.7/Cursor.Panel_0.0.7_x64.dmg) |
| Windows · x64 | [Installer for x64](https://github.com/devilcoolyue/cursor-panel/releases/download/v0.0.7/Cursor.Panel_0.0.7_x64-setup.exe) |

1. Install and open Cursor Panel. A local user and personal workspace are created automatically; Python, Node.js, and a server are not required.
2. Choose **Add account** (`添加账号`) and authorize it using your Cursor web session material. See the [account guide](docs/user-guide.md#accounts) for the supported input.
3. View quotas, open usage details, or choose **Switch** (`切换`) to use an account in the locally installed Cursor. Save your work before switching.

These are early preview packages without publisher signing or macOS notarization, so your OS may show an installation warning. Check the release's `SHA256SUMS` and follow the [installation and recovery guide](docs/v2-desktop-operations.md). See [supported platforms](docs/supported-platforms.md) for verification boundaries.

#### macOS: “damaged” or “cannot be opened”

The current macOS app has not been signed with an Apple Developer ID or notarized by Apple. Gatekeeper may therefore report **“Cursor Panel is damaged and can't be opened”** or **“Apple could not verify…”** after downloading it. This message does not necessarily mean the download is corrupt. Update-package signatures are separate from Apple code signing and do not remove this warning.

1. Download the matching DMG from this project's [GitHub Releases](https://github.com/devilcoolyue/cursor-panel/releases) and compare its SHA-256 with `SHA256SUMS` from the same release. Open the DMG and drag **Cursor Panel.app** into **Applications** before following the steps below.
2. If macOS blocks an unidentified developer, try opening the app once, then go to **System Settings → Privacy & Security → Open Anyway** and confirm.
3. If it still reports **“damaged”**, and you have verified and trust this download, open **Terminal** and run:

   ```bash
   xattr -dr com.apple.quarantine "/Applications/Cursor Panel.app"
   open "/Applications/Cursor Panel.app"
   ```

   This removes the download quarantine attribute only from Cursor Panel and its bundled files. It does not disable Gatekeeper for other apps. Keep the quotes because the app name contains a space; replace the path if you installed it elsewhere.

4. If the `xattr` command reports **Permission denied** or **Operation not permitted**, rerun it with administrator privileges, then open the app:

   ```bash
   sudo xattr -dr com.apple.quarantine "/Applications/Cursor Panel.app"
   open "/Applications/Cursor Panel.app"
   ```

   Enter your Mac login password when prompted; Terminal shows no characters while you type. **No such file** means the installation path is wrong or the app has not been copied into Applications. **No such xattr** means that file has no quarantine attribute.

If the checksum does not match, download the package again instead of removing quarantine. If it matches but the app still cannot open, [report an issue](https://github.com/devilcoolyue/cursor-panel/issues) with your macOS version, chip architecture, and exact error message.

### Docker Compose — for teams and self-hosting

**Requirements:** Docker Engine, Docker Compose v2, a domain pointing to your server, and reachable ports **80 / 443**. Caddy handles HTTPS.

```bash
git clone https://github.com/devilcoolyue/cursor-panel.git
cd cursor-panel
cp deploy/v2/.env.example deploy/v2/.env
```

Edit `deploy/v2/.env` and set `CURSOR_PANEL_DOMAIN` to your domain, such as `panel.example.com`, without a protocol or path. Then run these first-time setup commands from the repository root:

```bash
# Build the application and bundled Web interface.
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml build panel

# Create the encryption key and initial administrator.
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core --key-file /run/cursor-secrets/master.json keygen
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core server-init --login owner@example.com

# Start the application and HTTPS proxy.
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml up -d panel proxy
```

Replace `owner@example.com` with your administrator login. The terminal prompts twice for a password of at least 12 characters, with input hidden. Open `https://your-domain` and sign in with that account.

The database and master key use separate persistent volumes. Run a **single application process** per instance. Stop the application before maintenance, and back up both the database and matching key before updating. Existing installations should follow the [update and recovery procedure](docs/v2-web-operations.md), rather than repeat initialization.

### Try the interface with demo data

To explore before adding accounts, run the isolated preview from a source checkout. It requires **Python 3.10+**, **[uv](https://docs.astral.sh/uv/)**, and **Node.js 22.12+**.

```bash
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run --frozen python dev/preview-v2.py --port 18763
```

Open **<http://127.0.0.1:18763>**. Sign in as `owner@example.test`, `member@example.test`, or `viewer@example.test`, all with the password `Preview password 42!`.

The preview uses a temporary database, temporary keys, and synthetic accounts. It does not call Cursor's services, and preview switching scripts stop before accessing the local Cursor installation.

## Team access, explained

Each user gets a private personal workspace. For collaboration, create a team in the Web app, invite members, add accounts to that team, and assign access:

| Role or grant | What it allows |
| --- | --- |
| **Owner / Admin** | Manage team accounts, members, and account grants; only the Owner can transfer ownership |
| **Member / Viewer** | No team accounts are visible by default; access requires an account grant |
| **`view` grant** | Read the account's quota snapshot and usage details |
| **`use` grant** | Includes `view`, plus refresh and the supported switching flow |
| **Instance administrator** | Manage instance users and instance audit records; does not automatically gain access to other users' spaces |

`use` access allows the holder to obtain account credentials. Revoking a grant blocks future access through the panel, but cannot recall credentials already copied. Details: [roles and permissions](docs/user-guide.md#permissions).

## Command line

After `uv sync --locked`, run these commands from the repository root:

```bash
uv run --frozen cursor-remote --server https://panel.example.com --login owner@example.com workspaces
uv run --frozen cursor-remote --server https://panel.example.com --login owner@example.com list
uv run --frozen cursor-remote --server https://panel.example.com --login owner@example.com \
  detail --workspace WORKSPACE_UUID --account ACCOUNT_UUID
```

Use your server address and login; replace the UUID placeholders with IDs returned by the workspace and account lists. Each command prompts for a password, keeps its session in memory, and attempts to revoke it on exit.

`cursor-api` serves the authenticated API and built Web UI. `cursor-core` is the offline maintenance tool for initialization, migration, backup, and recovery; its Actor arguments do not replace remote authentication. See the [API guide](docs/v2-api-operations.md) and [core operations](docs/core-operations.md).

## Current scope

- **Quota data is a snapshot.** Failed refreshes preserve the last successful result and its timestamp. Percentages follow Cursor's response; dollar limits may be inferred. The data comes from non-public interfaces and is not an official billing statement or a compatibility guarantee.
- **Refresh behavior depends on the entry point.** The local desktop can refresh in the background when tray mode is enabled. The V2 server currently refreshes manually or on demand; it has no periodic quota scheduler.
- **Remote desktop switching remains disabled.** Remote viewing, management, and device login are available. Real Cursor session renewal and sharing across devices still need validation; synthetic tests do not establish that support.
- **Updates require published update packages.** Source includes update checks and signed update support. Signed update packages are included in v0.0.7; server upgrades also need the optional Linux amd64 host updater. Users of v0.0.1 must install v0.0.2 manually once to get the updater. See [automatic updates](docs/automatic-updates.md).
- **Platform coverage is explicit.** Desktop targets macOS arm64 / x64 and Windows x64. Linux native desktop and Windows arm64 are outside the current supported scope. See the [platform matrix](docs/supported-platforms.md) for tested environments and remaining checks.

## Technology

| Layer | Stack |
| --- | --- |
| Business core and API | Python 3.10+, FastAPI, Pydantic |
| Web interface | Vue 3, TypeScript, Vite |
| Desktop | Tauri 2 / Rust with a bundled Python backend |
| Persistence | SQLite, SQLAlchemy, Alembic; encrypted account credentials |
| Deployment | Docker Compose, Caddy HTTPS |

<details>
<summary>Project structure</summary>

```text
cursor-panel/
├── cursor_dashboard/       # Shared Python core, API, CLI, and local integrations
│   ├── api/               # Authenticated HTTP API
│   ├── application/       # Accounts, workspaces, identity, and access rules
│   ├── infrastructure/    # Persistence, encryption, and migrations
│   ├── local/             # Desktop runtime and local Cursor switching
│   └── runtime/           # Server and maintenance entry points
├── frontend/              # Shared Vue interface for Web and desktop
├── desktop/               # Tauri shell and bundled Python backend
├── deploy/v2/             # Compose, HTTPS proxy, and optional updater
├── dev/                   # Synthetic previews, builds, and release tooling
├── tests/                 # Core and integration tests
└── docs/                  # Guides, architecture decisions, and verification
```

</details>

## Development

Use **Node.js 22.13+** for the full test suite, including Node SQLite checks; CI uses Node 24. Desktop builds additionally require Rust and the target platform's build tools.

After installing dependencies as shown in the demo setup:

```bash
uv run --frozen python -m unittest discover -s tests -v
npm --prefix frontend run build
npx --prefix frontend playwright install chromium
npm --prefix frontend run test:e2e
npm --prefix frontend run test:connected
uv run --frozen python dev/release.py check
```

For a source-based Web installation, run `uv run --frozen python dev/build-web.py`, then initialize and start `cursor-api` as described in the [API guide](docs/v2-api-operations.md). Desktop build instructions are in [desktop/README.md](desktop/README.md).

Verification uses temporary data and simulated gateways. API types are generated from OpenAPI and checked for drift in CI. Implementation history and evidence are kept in the [architecture plan](docs/plans/v2-architecture.md), [Web report](docs/plans/p3-verification.md), [desktop report](docs/plans/p4-verification.md), [connected mode report](docs/plans/p5-verification.md), and [release report](docs/plans/p6-verification.md).

## Documentation

Both README editions cover the same setup and features. The detailed guides below are currently in **Chinese**; the desktop also provides a six-step introduction and searchable offline help.

| Guide | Contents |
| --- | --- |
| [User guide](docs/user-guide.md) | First steps, accounts, quotas, switching, and teams |
| [Web deployment](docs/v2-web-operations.md) | Docker, HTTPS, backup / restore, and remote CLI |
| [Desktop operations](docs/v2-desktop-operations.md) | Local setup, Cursor paths, switching, and encrypted archives |
| [Remote connections](docs/v2-connected-operations.md) | Browser login, saved instances, and device sessions |
| [API reference](docs/v2-api-operations.md) | Authentication, endpoints, and client conventions |
| [Core operations](docs/core-operations.md) | Keys, offline maintenance, and legacy migration |
| [Updates](docs/automatic-updates.md) | Desktop updates and optional server upgrade service |
| [Release operations](docs/v2-release-operations.md) | Checksums, manual updates, rollback, and signing |
| [Supported platforms](docs/supported-platforms.md) | Verified systems and current limitations |
| [Contributing](CONTRIBUTING.md) | Development workflow, checks, and issue reports |

<details>
<summary>Coming from the legacy version?</summary>

`cursor-panel` and `cursor-quota` retain the legacy shared panel and query interface. They use separate storage and authentication from V2; old `PANEL_TOKEN` values cannot authenticate to V2. The legacy source baseline is preserved on the remote `legacy` branch.

Follow the [legacy usage guide](docs/legacy-usage.md), [legacy deployment guide](docs/operations.md), and [maintenance notes](docs/maintenance.md). Use the [migration procedure](docs/core-operations.md) to move data into V2, and never open a V2 database with legacy code.

</details>

## Contributing

Bug reports, documentation improvements, and focused pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), and include your version, platform, reproduction steps, and sanitized errors when [reporting an issue](https://github.com/devilcoolyue/cursor-panel/issues). Keep real credentials, keys, databases, and generated switching commands out of commits and screenshots.

## License

[MIT](LICENSE) · Copyright © 2026 Cursor Panel contributors.

<div align="center">

If Cursor Panel makes managing your accounts easier, give the project a ⭐.

</div>

## Friend Links

- [LINUX DO](https://linux.do)

## Star History

<a href="https://star-history.com/#devilcoolyue/cursor-panel&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=devilcoolyue/cursor-panel&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=devilcoolyue/cursor-panel&type=Date" />
    <img alt="Cursor Panel Star History Chart" src="https://api.star-history.com/svg?repos=devilcoolyue/cursor-panel&type=Date" />
  </picture>
</a>
