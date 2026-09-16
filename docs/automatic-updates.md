# 版本显示与自动升级

左上角品牌名称旁显示当前版本徽标，点击展开版本信息、手动检查和发行记录。发现新版时，徽标变为黄色并显示呼吸小点，点击“查看更新”进入升级页面；个人设置、登录页和帮助菜单也提供入口。Web 页面分别显示页面版本和服务器版本。版本来自各自的构建包，构建时间用于区分同版本本地构建。

默认首次打开客户端或登录 Web 后检查一次，此后每 4 小时检查。检查时间及最近成功结果缓存在本机，刷新或重开不会重复查询未到期的版本；恢复联网或页面重新可见时会补查已到期的更新。自动检查只提醒，不自动安装。检查失败保留上次成功结果；从缓存恢复的客户端更新信息在安装前会重新校验。系统开启“减少动态效果”时，提示点保持静态。

检查更新读取本项目 GitHub 的最新正式 Release，以数字比较版本，不自动安装旧版本或预发布版本。网络失败显示可重试错误，不会显示为“已是最新版本”。服务器已更新而页面仍是旧版本时，可以直接刷新页面。

仓库统一为 [Cursor Panel](https://github.com/devilcoolyue/cursor-panel)，旧客户端通过 GitHub 重定向继续访问更新。签名更新清单保留旧下载路径，具体规则见[仓库更名与旧版兼容](repository-rename.md)。

## 客户端

点击「检查更新」，发现带有本平台更新包的新版本后，点击「一键升级并重启」。应用展示下载进度，验证更新签名，等待本地后台完成退出，将应用数据目录复制到系统应用缓存目录的 `update-backups/`，再安装、重启。完整升级备份最多保留 3 份、合计预算 1 GiB，始终保留本次恢复点，即使它单独超出预算；超出 24 小时的中断临时副本在下次升级时清理。只处理本程序命名的目录，不跟随符号链接。账号切换进行中时拒绝安装；备份、清理、下载或签名验证失败时保留当前应用。系统凭证库的原密钥继续使用，不写入更新包或备份副本。

更新使用 Tauri updater，支持 macOS arm64/x64 和 Windows x64。只有带有 `latest.json` 和签名更新包的正式版本才能自动安装。没有自动更新包的历史版本显示发行记录入口，不将打开下载页冒充安装完成。独立更新签名用于确认包来源，不等于 Apple Developer ID、公证或 Windows 发行者签名；相关发行要求仍见 [发行运维](v2-release-operations.md)。

## Web 服务器

普通用户可以查看版本、检查更新；只有实例管理员的网页会话可以提交服务器升级，接口同时验证权限、Origin 和 CSRF。远程设备会话不能提交服务器升级。自动升级支持仓库的 Linux amd64 Docker Compose 部署；其他部署仍可检查版本并按既有文档升级。

服务器需要**一次性安装宿主机更新服务**。网页容器只挂载任务队列，不获得 Docker socket 或任意命令执行入口。以下保留已有部署的 `/opt/cursor-dashboard` 路径；仓库更名不要求移动安装目录。新克隆若位于 `/opt/cursor-panel` 或其他位置，须同步修改下方命令及 service 文件中的绝对路径：

```bash
cd /opt/cursor-dashboard
uv sync --locked
sudo install -d -o root -g 10001 -m 2770 /var/lib/cursor-panel-updates
sudo cp deploy/v2/cursor-panel-updater.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cursor-panel-updater
docker compose --env-file deploy/v2/.env \
  -f deploy/v2/compose.yaml -f deploy/v2/compose.updates.yaml up -d panel proxy
```

运行 `uv sync` 的环境需要能被 service 中的 Python 绝对路径访问。服务由 root 运行以管理 Docker 和专用队列；队列的共享组为容器用户的 GID `10001`，默认的只读 Web 文件系统不变。`.env` 的 `CURSOR_PANEL_DATA_VOLUME` 必须指向已有数据卷，默认是标准部署的 `cursor-panel-v2_data`。已有自定义 Compose 项目需要填入真实卷名后再启动，不可用空新卷替代旧数据。

界面显示“自动升级服务已就绪”后即可使用。一次升级按如下顺序执行：

1. 从固定项目 Release 获取签名清单，验证签名、目标平台、版本、镜像 ID、大小和 SHA-256，完成下载后才停止服务。
2. 停止代理和应用，将原数据卷复制到新的候选卷。原数据卷、密钥卷和镜像保留；私有恢复记录保存原镜像的不可变 ID。
3. 在候选卷上执行 `cursor-core upgrade` 和 `verify`，成功后切换镜像和候选卷、执行启动健康检查，最后重新开放代理流量。
4. 迁移或健康检查失败时切回原镜像与原数据卷；恢复记录支持更新服务中断后继续回退。不会让旧程序读取已升级的数据库。

升级期间 Web 会短暂断开，页面会轮询连接并显示结果。升级完成后点击“刷新到新版本”。更新器在私有状态目录 `retained/` 登记恢复资源，默认保留最近 2 个成功升级的恢复点及其 `.env.before-*` 配置。超过上限时，仅清理带有本更新器所有权标签、且没有任何容器（包括已停止容器）引用的数据卷；镜像删除不使用强制选项，其他容器或标签使用的镜像受到 Docker 保护。首次部署的无标签原始卷/镜像、旧版本更新器留下的未登记资源和人工备份继续由维护者管理。

失败下载/升级产生的已登记候选资源会在确认回滚完成后回收；存在 `recovery.json` 时暂停清理。清理失败保留登记信息，每小时重试，不影响已完成的升级。网络下载、签名校验及候选准备失败不会停止当前服务；自动回退也失败时界面和 `status.json` 提示人工恢复，原数据仍被保留。升级后应同时更新宿主机上的 updater 代码/依赖并重启其 systemd 服务，旧 updater 不会自动获得新的清理策略。

## 发布可自动安装的版本

常规发版使用[固定发布流程](release-automation.md)：`dev/release_cli.py start --publish` 只启动 `release.yml` 一套流水线，同一份构建产物完成安装验证和更新签名，上传中断可恢复。`prepare` 更新版本与文档，`status` 返回简短进度。不要再把 P4/P5/P6 和旧更新包工作流一起启动。

`.github/workflows/update-packages.yml` 从指定版本 tag 构建签名更新产物，仅上传 CI artifact。维护者完成校验后，将 `signed-update-release` 的所有文件添加到相同版本 GitHub Release，最后发布该 Release。不要覆盖已经发布的版本或复用旧版本号。

更新验证公钥位于 `cursor_dashboard/updates/public-key.txt`，与 `desktop/src-tauri/tauri.conf.json` 的 updater 公钥一致。私钥必须保存在仓库外；CI 使用 `TAURI_SIGNING_PRIVATE_KEY` 和 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` secrets。首次配置所生成的本机私钥位于 `~/.local/share/cursor-panel-release/update.key`，权限为 `0600`；请按维护者密钥管理方式备份，禁止提交到源码。没有匹配私钥时，工作流拒绝生成更新包。

本地构建可用 `TAURI_SIGNING_PRIVATE_KEY_PATH` 指向私钥后运行 `dev/update-manifest.py`：

```bash
uv run --frozen python dev/update-manifest.py desktop --tag v0.0.8 \
  --target darwin-aarch64 --artifact 'desktop/src-tauri/target/release/bundle/macos/Cursor Panel.app' \
  --output output/updates
uv run --frozen python dev/update-manifest.py server --tag v0.0.8 \
  --artifact output/image.tar --image-metadata output/image.json --output output/updates
uv run --frozen python dev/update-manifest.py merge --tag v0.0.8 --output output/updates
```

示例版本必须与所有包的版本一致。macOS `.app` 必须已通过本地签名完整性校验；Windows 输入 NSIS 安装器。`merge` 要求三个桌面平台及服务器清单齐全并验证签名。`v0.0.8` 随发布提供这些更新产物。`v0.0.1` 未内置更新器，用户需先手动安装一次 `v0.0.2`；后续版本可通过应用内更新。

## 验证

```bash
uv run --frozen python -m unittest discover -s tests -p test_updates.py -v
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml --bin cursor-panel-desktop
npm --prefix frontend run build
npm --prefix frontend run test:updates
```

测试覆盖签名与篡改、版本比较、任务互斥、权限和 CSRF、迁移/健康失败回退、网页/客户端进度及重试。Linux 容器迁移流程使用可控的 Docker 适配器测试；真实发行仍须对最终发布包和目标主机做升级验收。
