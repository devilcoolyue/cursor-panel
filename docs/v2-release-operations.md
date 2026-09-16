# V2 候选交付、更新与回退

当前包版本为 `0.0.8`，API 主版本为 `1`，数据库为 `0004_retention`。版本号不代表完成了全部 V2 阶段：P0–P4 已有阶段验证，P5 远程查看/管理可用，真实远程切换仍关闭。P6 的验证结果见 [报告](plans/p6-verification.md)，平台范围见 [支持表](supported-platforms.md)。

## 候选产物与来源

常规正式发版现在采用[固定发布流程](release-automation.md)的 `release.yml`，集中完成测试、一次构建、签名与发布。下面的 P6 流程保留为手动候选交付和历史操作参考，不再随 `main` 推送自动构建所有平台。

`v0.0.8` 的变更与升级说明见[本次版本归档](archive/v0.0.8.md)，旧版附件保留在 [v0.0.7 归档](archive/v0.0.7.md)。P6 工作流可手动构建交付文件，P5 可单独验证三平台页面流程；手动交付时，维护者下载并验证本次提交的全部产物后，将其连同源码 ZIP/tar.gz、验证报告和总 SHA-256 发布至对应 GitHub Release。此步骤不改变桌面产物的未签名状态。

[P6 工作流](../.github/workflows/p6-release.yml) 从干净提交构建包含 Web 的 wheel、Linux amd64 镜像归档、macOS arm64/x64 DMG 和 Windows x64 NSIS。它只上传 CI artifact，不推送镜像、不创建 Release、不访问签名密钥。桌面候选不具有发行者签名/公证，不等同于正式安装发行。

每个目标目录包含安装文件、MIT `LICENSE`、`release-manifest.json` 和 `SHA256SUMS`。许可全文也进入校验文件清单，manifest 声明 `license=MIT`；项目许可证不替代第三方依赖原有许可。清单记录包版本、`v2-preview` 通道、目标架构、完整源码提交、CI run/attempt、依赖锁摘要、工具版本、API/schema、签名状态以及文件大小和 SHA-256。容器另记录 image ID、仓库 digest（如果存在）和 OCI 来源标签。候选的标识是版本、完整提交与构建运行，不能仅靠相同的 `0.0.1` 文件名区分新旧。

源码与依赖锁可追踪不代表字节级可复现：基础镜像标签、Rust stable、runner 和签名时间戳仍可能变化。正式发行应保存本次锁、清单、验证报告和实际镜像 digest；不要覆盖已有候选文件。

在已配置仓库权限的维护机上运行：

```bash
gh workflow run p6-release.yml --ref main
gh run list --workflow p6-release.yml
gh run download 运行ID --pattern 'p6-*' --dir /外部目录/p6-candidate
python3 dev/release.py verify /外部目录/p6-candidate/某个目标目录
```

`verify` 拒绝缺失、多余、重复、路径越界、符号链接、摘要和大小不一致的文件。也可在目标目录运行 macOS/Linux 的 `shasum -a 256 -c SHA256SUMS`，Windows 用 `Get-FileHash -Algorithm SHA256 文件名` 逐一与清单比较。校验和用于检测损坏；真实性仍依赖可信仓库、CI 来源和后续正式签名，不能把同目录的校验和当成数字签名。

本地验证示例：

```bash
uv sync --locked
npm --prefix frontend ci
uv run --frozen python dev/build-web.py
uv build --wheel --out-dir output/p6/wheel
uv run --frozen python dev/release.py check
uv run --frozen python dev/release.py manifest --target web-python \
  --artifact output/p6/wheel/cursor_dashboard-0.0.8-py3-none-any.whl \
  --output output/p6/web-python
python3 dev/release.py verify output/p6/web-python
```

候选打包默认拒绝脏工作区。本地未提交验证可显式加 `--allow-dirty`，清单会标记 `local_verification_only=true`；该产物不用于发行。输出目录必须不存在。正式候选从干净 checkout 构建，不能用本地缓存的旧安装包代替本次编译。

源文件检查仅读取 Git 跟踪的文件，拒绝运行库、私钥、已知令牌形态与越界路径，错误只报告文件名。wheel 检查 ZIP 成员与 Web 资源，镜像检查安装后的本项目包，桌面检查冻结后台资源树。它们是防止误带运行数据的自动防线，不能证明任意编码、压缩或所有格式中都不存在秘密；实际打包仍使用隔离 CI、显式资源路径和合成 fixture。不要将生产数据库、归档、凭证或生成的切换脚本带入构建目录。

## 服务端手动更新

1. 下载并验证目标候选，确认 API/schema、来源提交和平台。保存旧镜像及其 image ID/digest、旧配置和对应代码；不要用可变的 `latest` 表示回退目标。
2. 按 [Web 文档](v2-web-operations.md#更新备份与恢复) 停止 `panel`，用旧镜像的 `maintenance` 执行 `cursor-core backup /backups/新备份名`，把备份和匹配主密钥分别复制到外部安全位置。
3. `docker load --input 镜像归档.tar` 后核对 `docker image inspect` 的 image ID 与清单一致，再为该 ID 指定包含版本和提交的唯一标签。将 `deploy/v2/.env` 的 `CURSOR_PANEL_IMAGE` 改为该标签或已发布镜像的不可变 digest。panel 和 maintenance 共用此变量。
4. 用新镜像运行 `docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance cursor-core upgrade`，随后执行相同前缀的 `cursor-core verify`。两者成功后再 `up -d panel proxy`，登录检查空间、账号数和快照时间。
5. 失败时保持服务停止，保留失败库用于排查。将 data 改挂到新的空目录/卷，恢复原镜像、原配置和匹配密钥，用原版本 `cursor-core restore /backups/新备份名` 恢复，校验后启动。不运行 Alembic downgrade，不让旧程序打开已升级数据库。

旧 schema 的备份要用认识该 schema 的旧版本恢复；当前 `restore` 故意拒绝非当前 schema。回退恢复撤销备份中的网页/设备会话和切换票据，用户重新登录。恢复不撤销已经复制出去的上游凭证；升级之后若发生 RT 轮换，旧备份中的凭证可能需要重新授权。

源码/wheel 部署采用同样顺序：停进程，用旧环境备份，在独立虚拟环境安装已验证的新 wheel，用新环境升级/校验再切换服务命令。保留旧虚拟环境与备份，失败时在空数据目录用旧环境恢复。不要在运行中的环境直接覆盖依赖或数据库。

## 独立桌面手动更新

1. 在个人设置导出一个新的加密归档并保留口令。归档提供账号和主密钥恢复材料，不包含完整团队/会话/审计，不能代替完整目录备份。
2. 使用托盘“退出 Cursor Panel”完全退出，确认后台结束，再将 [应用数据目录](v2-desktop-operations.md#本地使用) 整体复制到受保护的外部备份位置；保存旧安装包。完全停止后复制整个目录，不能只复制仍在使用的 `core.db`。
3. 校验新安装包来源、架构与 SHA-256 后安装。首次启动在数据目录锁内检查原系统密钥，遇到旧 schema 先生成包含已提交 WAL 的 `pre-upgrade-*.db`，再升级。不能以删除系统凭证库条目或原数据库解决升级错误。
4. 检查本地账号、快照与远程连接。若升级失败，退出新程序、保留失败目录，重新安装原版本，将完整备份恢复到原应用数据路径并使用原系统密钥。应用路径和应用标识保持一致；若密钥丢失，使用之前的加密归档恢复。

自动 `pre-upgrade` 库是额外恢复副本，不包含完整目录配置或独立密钥。跨电脑使用加密归档导入到空个人空间；远程设备连接重新登录，必要时在服务端撤销旧设备。现在可通过「关于与更新」使用签名自动更新器，配置与发布步骤见[自动升级](automatic-updates.md)；仍不提供桌面一键 schema 降级。

## 连接远端与协议不兼容

桌面和远程 CLI 先检查 API 主版本；桌面还要求服务端声明 `device_sessions=true`。主版本不兼容时停止后续业务请求并提示升级，不能修改服务端能力声明来绕过检查。服务端与桌面应按各自支持表升级；未知可选字段可以忽略，未声明的可选能力不展示。`remote_switch=false` 持续生效，桌面连接升级不会启用真实切换。

## 正式签名方案

以下为发行实施方案，尚未用维护者证书实际签名、公证或完成干净机器验收。当前候选工作流没有签名密钥入口；正式签名需在隔离的受保护发行环境建立单独作业。

macOS：使用维护者 Apple Developer 的 Developer ID Application 身份，保持 `dev.cursor-panel.desktop` 和最低系统声明不变。先逐项签名冻结后台中的 Python 可执行文件、dylib/扩展及 framework，再签后台入口与外层 `.app`；启用 hardened runtime，仅按实际 Python/系统自动化需要配置最小 entitlements。Tauri 的 `bundle.macOS.signingIdentity`、`hardenedRuntime`、`entitlements` 由发行 overlay 指定，不能假定只签外壳就覆盖随包后台。

随后使用 `xcrun notarytool submit … --wait` 提交最终应用/DMG，检查 Accepted、staple 并执行 `xcrun stapler validate`。验收包含 `codesign --verify --deep --strict`、`spctl --assess --type execute` 以及保留下载隔离标记的全新用户安装；再次验证系统密钥持久化、更新后旧密钥访问、退出清理和默认浏览器回调。每种架构都要记录证书 Team ID、证书到期、notary submission ID 和验证结果。不要通过移除 quarantine 或关闭 Gatekeeper 作为发行验证。

Windows：使用维护者选择的受信任代码签名证书或云签名服务，固定发行者身份；以 SHA-256 Authenticode 和 RFC 3161 时间戳签名本项目随包后台、应用 EXE，再签最终 NSIS 安装器。Tauri `bundle.windows.certificateThumbprint` / `digestAlgorithm` / `timestampUrl` 或固定 `signCommand` 由发行配置指定；私钥放受保护凭证库/HSM，不能提交 PFX 或密码。验证 `signtool verify /pa /all /v` 和 `Get-AuthenticodeSignature`，并在干净系统记录安装、卸载、覆盖更新、WebView2 获取以及 SmartScreen 行为。签名成功不保证立即具备 SmartScreen 信誉。

签名/公证会改变文件字节。只有最终签名文件通过上述验证后，才能在单独的签名发行流程记录实际证据并重新生成 SHA-256/清单；当前 `dev/release.py` 仅生成 unsigned preview，不能用它宣称已签名。保留第三方依赖原有许可证与签名；项目许可证不替代依赖许可。

自动更新验证签名、目标平台、版本和下载完整性，并先备份再执行 schema 升级。HTTPS 或校验和本身不能替代更新签名。
