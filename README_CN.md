<div align="center">

<img src="frontend/src/icon.svg" alt="Cursor Panel 图标" width="112" height="112" />

# Cursor Panel

**多个 Cursor 账号，一眼掌握。**

集中查看额度、切换本机账号，让团队按需共享授权。<br />
个人使用独立客户端，团队部署自己的服务端。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Tauri](https://img.shields.io/badge/Tauri-2-24C8D8?logo=tauri&logoColor=white)](https://tauri.app/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](deploy/v2/compose.yaml)
[![Status](https://img.shields.io/badge/Status-V2%20preview-D4A34A)](docs/supported-platforms.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-93DBB5)](LICENSE)

[English](README.md) | **简体中文**

[下载安装](https://github.com/devilcoolyue/cursor-panel/releases) · [快速开始](#快速开始) · [使用文档](#使用文档) · [反馈问题](https://github.com/devilcoolyue/cursor-panel/issues)

</div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/dashboard-dark.png" />
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/dashboard-light.png" />
  <img src="docs/assets/dashboard-light.png" alt="Cursor Panel 团队空间：三个账号的剩余额度、账单周期与标签筛选" width="1440" />
</picture>

<p align="center"><sub>当前 V2 预览界面 · 液态玻璃皮肤 · 全部使用模拟账号 · 应用界面目前为中文。</sub></p>

## 为什么选择 Cursor Panel？

- **额度一目了然。** 集中查看套餐、账单周期、Cursor / Other Models 剩余额度和 Grok 周用量，点开账号即可查询模型用量明细。
- **账号井井有条。** 自定义名称与标签，按需搜索、排序；个人空间与团队空间分开管理不同用途的账号。
- **在桌面直接切换。** macOS / Windows 客户端正常退出 Cursor、备份登录数据库、更新账号并重启，必要时可在设置中恢复备份。
- **团队按需共享。** 邀请成员、分配角色，为单个账号授予 `view` 或 `use` 权限，通过审计记录查看空间内的操作。
- **个人即装即用，团队随时连接。** 客户端无需服务端即可独立运行，也可保存多个远程实例，通过系统浏览器登录，设备会话随时可撤销。
- **凭证加密保存。** 账号凭证加密落盘，桌面密钥保存在系统凭证库；使用加密归档迁移账号或恢复原密钥。
- **界面由你决定。** 六种皮肤、独立明暗模式、自定义卡片内容、可折叠侧栏与响应式 Web 布局，内置新手指引帮助上手。

本文介绍当前 V2 预览源码。最新可下载版本为 [v0.0.8](https://github.com/devilcoolyue/cursor-panel/releases/tag/v0.0.8)，具体发布内容见[版本归档](docs/archive/v0.0.8.md)；截图使用模拟预览数据。

## 选择适合你的使用方式

| 使用入口 | 适用场景 | 账号存储位置 | Cursor 切换方式 |
| --- | --- | --- | --- |
| **独立客户端** | 管理自己电脑上的账号 | 本机数据库 | macOS / Windows 本机直接切换 |
| **自托管 Web** | 团队集中管理与共享账号 | 你的服务器 | 生成短时有效的 macOS / Windows 终端命令 |
| **客户端连接服务端** | 在多个实例间访问个人与团队空间 | 当前选中的服务器 | 远程桌面切换目前关闭 |
| **远程 CLI** | 在终端查询账号 | 你的服务器 | 提供查询与刷新操作 |

本地与远程数据库相互独立。切换客户端连接不会上传或同步本地账号。

## 快速开始

### 独立客户端：个人使用

从 **[v0.0.8 Releases](https://github.com/devilcoolyue/cursor-panel/releases/tag/v0.0.8)** 下载对应平台的安装包：

| 平台 | 下载 |
| --- | --- |
| macOS · Apple Silicon | [arm64 版 DMG](https://github.com/devilcoolyue/cursor-panel/releases/download/v0.0.8/Cursor.Panel_0.0.8_aarch64.dmg) |
| macOS · Intel | [x64 版 DMG](https://github.com/devilcoolyue/cursor-panel/releases/download/v0.0.8/Cursor.Panel_0.0.8_x64.dmg) |
| Windows · x64 | [x64 版安装程序](https://github.com/devilcoolyue/cursor-panel/releases/download/v0.0.8/Cursor.Panel_0.0.8_x64-setup.exe) |

1. 安装并打开 Cursor Panel，自动创建本地用户与个人空间，无需安装 Python、Node.js 或部署服务端。
2. 点击「添加账号」，使用 Cursor 网页会话材料完成授权，支持的输入方式见[账号使用指南](docs/user-guide.md#accounts)。
3. 查看额度、打开明细，或点击「切换」在本机已安装的 Cursor 中使用该账号；切换前先保存工作。

当前安装包为早期预览版本，尚无发行者签名或 macOS 公证，系统可能显示安装提示。请核对 Release 中的 `SHA256SUMS`，安装与恢复步骤见[桌面使用说明](docs/v2-desktop-operations.md)，实测范围见[支持平台](docs/supported-platforms.md)。

#### macOS 提示「已损坏」或「无法打开」怎么办？

当前 macOS 应用尚未使用 Apple Developer ID 签名，也未完成 Apple 公证。下载后，Gatekeeper 可能提示 **「Cursor Panel 已损坏，无法打开」** 或 **「Apple 无法验证……」**，不一定是文件真的损坏。自动更新包的校验签名与 Apple 应用签名是两回事，不能消除这类提示。

1. 从本项目的 [GitHub Releases](https://github.com/devilcoolyue/cursor-panel/releases) 下载对应芯片架构的 DMG，并核对同一版本 `SHA256SUMS` 中的 SHA-256。打开 DMG，先将 **Cursor Panel.app** 拖入**「应用程序」**文件夹，再执行下面的操作。
2. 如果提示无法验证开发者，先尝试打开一次应用，再前往**「系统设置 → 隐私与安全性 → 仍要打开」**，按系统提示确认。
3. 如果仍提示**「已损坏」**，确认安装包来源可信且校验一致后，打开**「终端」**，执行：

   ```bash
   xattr -dr com.apple.quarantine "/Applications/Cursor Panel.app"
   open "/Applications/Cursor Panel.app"
   ```

   这只会移除 Cursor Panel 及其随包文件的下载隔离标记，不会关闭其他应用的 Gatekeeper 检查。应用名称含空格，请保留双引号；如果安装在其他位置，请替换为实际路径。

4. 如果 `xattr` 提示 **Permission denied** 或 **Operation not permitted**，使用管理员权限重新执行，然后打开应用：

   ```bash
   sudo xattr -dr com.apple.quarantine "/Applications/Cursor Panel.app"
   open "/Applications/Cursor Panel.app"
   ```

   按提示输入 Mac 的登录密码；终端输入密码时不会显示任何字符，输完按回车即可。**No such file** 表示路径不正确，或尚未把应用复制到「应用程序」；**No such xattr** 表示对应文件已没有该隔离标记。

如果 SHA-256 不一致，请重新下载安装包，不要通过移除隔离标记继续打开。如果校验一致但仍无法启动，请[反馈问题](https://github.com/devilcoolyue/cursor-panel/issues)，附上 macOS 版本、芯片架构和完整报错。

### Docker Compose：团队与自托管

**准备条件：** Docker Engine、Docker Compose v2、指向服务器的域名，以及可达的 **80 / 443** 端口。HTTPS 由 Caddy 自动配置。

```bash
git clone https://github.com/devilcoolyue/cursor-panel.git
cd cursor-panel
cp deploy/v2/.env.example deploy/v2/.env
```

编辑 `deploy/v2/.env`，将 `CURSOR_PANEL_DOMAIN` 改为自己的域名，例如 `panel.example.com`，不含协议或路径。然后在仓库根目录执行首次初始化：

```bash
# 构建应用及随包 Web 界面。
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml build panel

# 创建加密主密钥与初始管理员。
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core --key-file /run/cursor-secrets/master.json keygen
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core server-init --login owner@example.com

# 启动应用与 HTTPS 代理。
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml up -d panel proxy
```

把 `owner@example.com` 替换为管理员登录邮箱。终端会要求两次输入至少 12 字符的密码，输入过程不回显。完成后访问 `https://你的域名`，使用该账号登录。

数据库与主密钥分别使用独立持久卷。每个实例运行**单个业务进程**；维护前停止应用，更新前同时备份数据库与匹配密钥。已有部署请按 [Web 更新与恢复流程](docs/v2-web-operations.md)操作，不要重复初始化。

### 使用模拟数据体验界面

想先体验再添加账号，可在源码仓库中启动隔离预览。需要 **Python 3.10+**、**[uv](https://docs.astral.sh/uv/)** 和 **Node.js 22.12+**。

```bash
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run --frozen python dev/preview-v2.py --port 18763
```

访问 **<http://127.0.0.1:18763>**。可使用 `owner@example.test`、`member@example.test` 或 `viewer@example.test` 登录，密码均为 `Preview password 42!`。

预览使用临时数据库、临时密钥与模拟账号，不请求 Cursor 服务；预览切换脚本会在访问本机 Cursor 前停止。

## 团队权限如何工作

每位用户都有独立的个人空间。需要协作时，在 Web 中创建团队、邀请成员，将账号添加到团队后再分配权限：

| 角色或授权 | 能做什么 |
| --- | --- |
| **Owner / Admin** | 管理团队账号、成员与账号授权；只有 Owner 可以转移所有权 |
| **Member / Viewer** | 默认看不到团队账号，获得单独的账号授权后才可访问 |
| **`view` 授权** | 查看该账号的额度快照与用量明细 |
| **`use` 授权** | 包含 `view`，还可刷新额度并使用已支持的切换方式 |
| **实例管理员** | 管理实例用户与实例审计，不自动获得其他用户空间的访问权 |

拥有 `use` 权限的人可以领取账号凭证。撤销授权会阻止后续通过面板访问，但不能收回已复制的凭证。详见[角色与权限说明](docs/user-guide.md#permissions)。

## 命令行

执行 `uv sync --locked` 后，在仓库根目录使用：

```bash
uv run --frozen cursor-remote --server https://panel.example.com --login owner@example.com workspaces
uv run --frozen cursor-remote --server https://panel.example.com --login owner@example.com list
uv run --frozen cursor-remote --server https://panel.example.com --login owner@example.com \
  detail --workspace WORKSPACE_UUID --account ACCOUNT_UUID
```

替换为自己的实例地址与登录邮箱；UUID 占位符使用空间、账号列表返回的 ID。每条命令隐藏输入密码，会话仅保存在内存中，结束时尝试撤销。

`cursor-api` 提供已认证 API 和构建后的 Web 界面。`cursor-core` 用于离线初始化、迁移、备份与恢复，其 Actor 参数不能替代远程用户登录。详见 [API 文档](docs/v2-api-operations.md)和[核心运维](docs/core-operations.md)。

## 当前支持范围

- **额度是快照数据。** 刷新失败保留最后成功结果与时间。百分比沿用 Cursor 返回口径，美元上限可能由数据推算；接口为非公开接口，不能将结果视为官方账单或兼容性承诺。
- **刷新方式取决于入口。** 本地客户端启用托盘模式后可在后台定期刷新；V2 服务端当前采用手工刷新与按需查询，尚无周期额度调度。
- **远程桌面切换仍关闭。** 已支持远程查看、管理与设备登录；真实 Cursor 续期及多设备共享会话仍待验证，模拟测试不等于这些能力已获实测支持。
- **自动升级需要配套发布产物。** 源码已接入更新检查与签名更新能力；v0.0.8 提供匹配的签名更新包，服务端还需配置可选的 Linux amd64 宿主机更新服务。v0.0.1 用户需先手动安装一次 v0.0.2，以获得更新器。详见[自动升级](docs/automatic-updates.md)。
- **平台范围明确。** 客户端面向 macOS arm64 / x64 与 Windows x64；Linux 原生桌面、Windows arm64 暂不在支持范围内。已验证环境与待验项目见[支持平台表](docs/supported-platforms.md)。

## 技术栈

| 层次 | 技术 |
| --- | --- |
| 业务核心与 API | Python 3.10+、FastAPI、Pydantic |
| Web 界面 | Vue 3、TypeScript、Vite |
| 桌面客户端 | Tauri 2 / Rust，随包携带 Python 后台 |
| 数据存储 | SQLite、SQLAlchemy、Alembic；账号凭证加密保存 |
| 部署 | Docker Compose、Caddy HTTPS |

<details>
<summary>项目结构</summary>

```text
cursor-panel/
├── cursor_dashboard/       # 共用 Python 核心、API、CLI 与本机集成
│   ├── api/               # 已认证 HTTP API
│   ├── application/       # 账号、空间、身份与权限规则
│   ├── infrastructure/    # 持久化、加密与数据库迁移
│   ├── local/             # 桌面运行与本机 Cursor 切换
│   └── runtime/           # 服务端及离线运维入口
├── frontend/              # Web 与桌面共用的 Vue 界面
├── desktop/               # Tauri 外壳与随包 Python 后台
├── deploy/v2/             # Compose、HTTPS 代理与可选更新服务
├── dev/                   # 模拟预览、构建与发布工具
├── tests/                 # 核心与集成测试
└── docs/                  # 使用指南、架构决策与验证记录
```

</details>

## 本地开发

完整测试（含 Node SQLite 检查）需要 **Node.js 22.13+**，CI 使用 Node 24。构建桌面另需 Rust 和目标系统的构建工具链。

按模拟预览步骤安装依赖后执行：

```bash
uv run --frozen python -m unittest discover -s tests -v
npm --prefix frontend run build
npx --prefix frontend playwright install chromium
npm --prefix frontend run test:e2e
npm --prefix frontend run test:connected
uv run --frozen python dev/release.py check
```

从源码运行 Web 时，执行 `uv run --frozen python dev/build-web.py`，再按 [API 运行文档](docs/v2-api-operations.md)初始化并启动 `cursor-api`。桌面构建步骤见 [desktop/README.md](desktop/README.md)。

验证使用临时数据与模拟网关；API 类型由 OpenAPI 生成，CI 检查类型漂移。实施过程与验证依据保存在[架构计划](docs/plans/v2-architecture.md)、[Web 报告](docs/plans/p3-verification.md)、[桌面报告](docs/plans/p4-verification.md)、[远程连接报告](docs/plans/p5-verification.md)和[发布报告](docs/plans/p6-verification.md)中。

## 使用文档

中英文 README 覆盖相同的功能与上手流程，以下详细指南目前以**中文**提供。客户端还内置六步新手指引与可搜索的离线帮助文档。

| 文档 | 内容 |
| --- | --- |
| [完整使用指南](docs/user-guide.md) | 新手入门、账号、额度、切换与团队 |
| [Web 部署与使用](docs/v2-web-operations.md) | Docker、HTTPS、备份恢复与远程 CLI |
| [桌面使用说明](docs/v2-desktop-operations.md) | 本地设置、Cursor 路径、切换与加密归档 |
| [连接远程实例](docs/v2-connected-operations.md) | 浏览器登录、实例管理与设备会话 |
| [API 参考](docs/v2-api-operations.md) | 身份认证、接口与客户端约定 |
| [核心运维](docs/core-operations.md) | 密钥、离线维护与旧版迁移 |
| [自动升级](docs/automatic-updates.md) | 客户端更新与可选的服务端升级服务 |
| [发布与维护](docs/v2-release-operations.md) | 校验、手动更新、回退与签名 |
| [支持平台](docs/supported-platforms.md) | 已验证系统与当前限制 |
| [贡献指南](CONTRIBUTING.md) | 开发流程、检查与问题报告 |

<details>
<summary>从旧版迁移？</summary>

`cursor-panel` / `cursor-quota` 继续提供旧版共享面板与查询入口，其存储与认证独立于 V2；旧 `PANEL_TOKEN` 不能访问 V2。旧版源码基线保存在远端 `legacy` 分支。

旧版操作见[使用说明](docs/legacy-usage.md)、[部署说明](docs/operations.md)和[维护文档](docs/maintenance.md)。迁入 V2 请按[核心迁移流程](docs/core-operations.md)操作，不要使用旧程序打开 V2 数据库。

</details>

## 参与贡献

欢迎提交问题、改进文档或发起聚焦具体问题的 PR。请先阅读[贡献指南](CONTRIBUTING.md)；[反馈问题](https://github.com/devilcoolyue/cursor-panel/issues)时附上版本、平台、复现步骤和脱敏错误。真实凭证、密钥、数据库与生成的切换命令不应出现在提交或截图中。

## 许可证

[MIT](LICENSE) · Copyright © 2026 Cursor Panel contributors.

<div align="center">

如果 Cursor Panel 让你的账号管理更轻松，欢迎点一个 ⭐。

</div>

## 友情链接

- [LINUX DO](https://linux.do) - 新的理想型社区

## Star 趋势

<a href="https://star-history.com/#devilcoolyue/cursor-panel&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=devilcoolyue/cursor-panel&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=devilcoolyue/cursor-panel&type=Date" />
    <img alt="Cursor Panel Star 趋势图" src="https://api.star-history.com/svg?repos=devilcoolyue/cursor-panel&type=Date" />
  </picture>
</a>
