# 支持平台与验证边界

当前版本 `0.0.9` / V2 preview。以下“通过”均指报告中的隔离合成测试，不等同于真实 Cursor 会话或所有干净设备验证。当前版本的构建与验证记录随 [GitHub Release](https://github.com/devilcoolyue/cursor-panel/releases/tag/v0.0.9) 提供；历史 P6 结果见 [验证报告](plans/p6-verification.md)，下表保留 P5 更完整页面/原生适配的历史依据。

| 入口 / 平台 | 有记录的验证 | 当前边界 |
| --- | --- | --- |
| Web / Linux amd64 服务端 | P5 [服务端 CI 34469364655](https://github.com/devilcoolyue/cursor-panel/actions/runs/34469364655)：镜像启动、持久化、备份恢复、权限与 wheel | 单实例单进程；Linux arm64 本轮结果单独记录；无集群支持 |
| Web / Linux arm64 容器 | P6 本机 Docker Desktop：启动、持久化、备份/恢复、会话撤销及镜像归档校验 | macOS arm64 宿主上的 Linux 容器；独立 arm64 Linux 宿主未验 |
| Python API / CLI | 同次 CI：Linux Python 3.10，macOS/Windows Python 3.12 | Python 元数据要求 3.10+；其他解释器版本不能仅凭约束推断通过 |
| Web 页面 / Chromium | P5 [页面 CI 34469364723](https://github.com/devilcoolyue/cursor-panel/actions/runs/34469364723)：macOS arm64/x64、Windows x64 | 含移动视口；Safari、Firefox、实际移动设备未单独验证 |
| 桌面 / macOS arm64 | P5 [桌面 CI 34469364709](https://github.com/devilcoolyue/cursor-panel/actions/runs/34469364709)，macos-14 runner：DMG/.app、重定位启动、系统密钥、原生 fixture、异常清理 | 配置最低 macOS 12.0，最低版本未实测；未签名/公证，真实 Cursor 待验 |
| 桌面 / macOS x64 | 同次桌面 CI，macos-15-intel runner | 同上；不是 universal 合并包 |
| 桌面 / Windows x64 | 同次桌面 CI，windows-latest runner：NSIS 实际安装后启动、系统凭证库、原生 fixture、异常清理 | WebView2 由 Tauri 安装器处理；全新系统下载链路、最低 Windows 版本、正式签名未验 |
| 远程连接 / 上述三种桌面 | 同次桌面/页面 CI：PKCE、loopback HTTP、撤销、权限和兼容提示 | Chromium 适配 IPC 的页面测试不能替代全部默认浏览器/原生 WebView 组合 |
| 真实远程 Cursor 切换 | 无 S01–S06 完整证据 | 生产关闭，不在支持范围 |
| 自定义/便携 Cursor 数据目录与 Windows 扩展路径发现 | 本机合成安装、数据库和进程测试；Chromium 路径设置流程 | 已实现兼容入口；实际 Windows 安装、快捷方式发现及真实 Cursor 切换待验 |
| Linux 原生桌面、Windows arm64 | 无 | 不提供支持声明 |

用户安装桌面产物无需 Python、Node 或 uv。源码构建需要 Rust、Node、uv；前端工具最低 Node 22.12，完整 Node SQLite 回归需要 22.13+，CI 使用 Node 24。移除开发工具 PATH 的安装探测证明随包运行时独立性，不代表全新系统已全部验收。

新提交的 CI 与历史 P5 记录分别引用，不用旧提交通过的结果替代新发布候选检查。后续扩大支持范围需补实际目标平台、提交、运行 ID 与失败/跳过记录。
