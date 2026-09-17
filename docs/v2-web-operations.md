# V2 Web 部署与使用

P3 在已认证 V2 API 上提供 Vue 界面、手工切换、远程 CLI 和容器交付。账号/空间/身份规则见 [API 运行说明](v2-api-operations.md)，迁移旧版数据见 [核心运维](core-operations.md)。`cursor-panel` / `cursor-quota` 仍是 legacy 入口，V2 使用独立数据库和独立主密钥。

## Docker Compose 首次部署

需要 Docker Engine、Compose v2、指向服务器的域名，以及可达的 80/443 端口。首次构建需要网络下载 Python/Node 依赖；最终镜像包含构建后的界面，不依赖 CDN、Node 或宿主 Python。

在仓库根目录执行：

```bash
cp deploy/v2/.env.example deploy/v2/.env
# 编辑 deploy/v2/.env，将 CURSOR_PANEL_DOMAIN 改成自己的域名（不含协议/路径）。
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml build panel
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core --key-file /run/cursor-secrets/master.json keygen
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core server-init --login owner@example.com
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml up -d panel proxy
```

初始化在终端隐藏输入两次至少 12 字符的密码。初始化成功后访问 `https://你的域名` 登录。不存在浏览器抢占首个管理员的注册接口；页面提供首次使用说明。已有密钥或已初始化实例会拒绝重复创建，不覆盖旧配置。

Compose 使用独立命名卷：`data` 保存数据库，`secrets` 保存主密钥，`backups` 保存离线数据库备份。运行中的服务只读挂载密钥，容器以 UID/GID 10001 运行，根文件系统只读。维护容器以相同用户运行并可写密钥卷。首次使用命名卷会继承镜像中目录所有权。

如改用宿主目录挂载，预先建立目录并赋予 UID 10001 访问权限，密钥文件必须为 0600；不要把运行数据或密钥放进源码目录/镜像构建上下文。

Caddy 自动申请 HTTPS 证书，保持 Host，并将请求转发到内部 8000 端口。业务端口不发布到宿主。标准 Compose 使用独立网段 `172.30.87.0/24`，将 Caddy 固定为 `172.30.87.2`，后端只信任该地址的转发头；Caddy 覆盖 X-Forwarded-For 为实际连接来源，客户端不能伪造限流身份。网段冲突时，在 `.env` 中同时修改 `CURSOR_PANEL_SUBNET`、`CURSOR_PANEL_DYNAMIC_RANGE` 和 `CURSOR_PANEL_PROXY_IP`；动态分配段默认 `172.30.87.128/25`，必须属于网段且不包含代理固定 IP，避免业务容器先启动时占用代理地址。

若使用已有代理，只启动 panel，让代理加入同一网络，并将 `CURSOR_TRUSTED_PROXIES` 指定为其实际 IP 或受控 CIDR；源码运行可用同名环境变量或 `cursor-api --trusted-proxies`，默认空值不信任转发头，拒绝 `*` 和全网 CIDR。保持原 Host，设置精确 HTTPS origin，禁止代理记录 Cookie、请求体和完整查询串。前面还有 CDN/负载均衡时，需在可信边界内配置真实来源解析，不能直接透传客户端提供的转发头。

panel、proxy、maintenance 均配置 Docker `json-file` 日志轮转：每份 10 MiB、最多 5 份。应用仍关闭访问日志以避免记录切换票据。已有容器需要重新创建才会采用新日志配置；旧容器日志不由应用直接删除。更改 Compose 网段同样需要维护窗口重建网络和容器，保留命名数据卷，不能使用 `down -v`。

V2 每分钟分批清理审计和过期认证记录；审计默认保留最近 90 天，且全实例最多保留 100,000 条，任一上限达到即从最旧记录开始删除。可通过 `CURSOR_AUDIT_RETENTION_DAYS`、`CURSOR_AUDIT_MAX_EVENTS` 设置正整数；后台按每批最多 5,000 条处理已有积压。需要更长审计历史时先备份或外部归档。普通删除回收的 SQLite 页面会被后续写入复用；需要缩小历史大文件时，停止 panel 后运行 `cursor-core maintain --compact`，该命令持有数据目录锁并执行清理、checkpoint 和 VACUUM。应用与维护容器使用相同的审计保留配置。

`GET /api/v1/health` 检查数据库可读性，所有入口仍检查 Host/Origin。Docker 健康检查在容器内使用配置的 Host。服务启动前校验密钥、schema 和认证初始化状态。

首版只支持一个实例、一个业务进程和一个数据目录写入者。不要增加 uvicorn workers 或复制 panel 服务。维护命令必须先停止 panel；进程锁会阻止在线升级、备份和第二个业务进程。

版本/校验、镜像固定、schema 失败恢复和签名方案见 [发行运维](v2-release-operations.md)，当前平台证据见 [支持表](supported-platforms.md)。`CURSOR_PANEL_IMAGE` 同时控制 panel 和 maintenance，更新时为两者指定同一已验证镜像。

## 更新、备份与恢复

更新不会隐式升级数据库。先停止服务并备份，再用新镜像执行显式升级：

```bash
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml stop panel
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance \
  cursor-core backup /backups/before-update
# 另外安全备份 secrets 卷中的原 master.json，离开容器卷存放。
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml build panel
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml run --rm maintenance cursor-core upgrade
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml up -d panel proxy
```

备份名称必须未被使用。`backup` 在数据目录锁内校验可解密性，再用 SQLite backup API 生成包含已提交 WAL 数据的一致副本和仅含元数据的 manifest。备份不内嵌主密钥；数据库和匹配的密钥缺一不可。卷仍在同一机器上不能防止磁盘丢失，应把数据库备份和独立密钥副本分别安全复制到其他位置。

从容器卷取出备份的一个办法是建立停止状态的维护容器，再用 Docker 的文件复制功能（不会输出文件内容）：

```bash
docker compose --env-file deploy/v2/.env -f deploy/v2/compose.yaml create maintenance
# 用 docker compose ... ps -a -q maintenance 取得容器 ID。
# 预先建立权限为 0700 的外部备份目录。
docker cp 容器ID:/backups/before-update /安全备份目录/
docker cp 容器ID:/run/cursor-secrets/master.json /独立密钥备份目录/master.json
chmod 600 /独立密钥备份目录/master.json
```

新环境恢复：准备原数据库备份和原密钥，启动前执行下面等价命令。`--data-dir` 必须是空目录；恢复不会覆盖已有实例。恢复先在临时目录校验 schema、完整性和全部凭证解密，再生成目标库，并撤销备份中的全部会话和切换票据。

```bash
cursor-core --data-dir /新环境/data --key-file /恢复的密钥/master.json \
  restore /恢复的备份/before-update
cursor-core --data-dir /新环境/data --key-file /恢复的密钥/master.json verify
```

容器中用 maintenance 的 `cursor-core restore /backups/before-update`，挂载到新的空 data 卷和原 secrets 卷。密钥错误、目标非空、schema 不匹配或锁被占用时拒绝恢复。恢复后用户重新登录，角色和账号授权保留。升级失败时停止新程序，使用原镜像和备份恢复到空数据目录；不承诺 schema 反向降级。已发生上游凭证轮换时，恢复的旧凭证可能需要重新授权。

## Web 流程

- 登录后默认进入个人空间，账号只对本人可见。团队协作入口默认折叠；创建团队不会自动共享个人账号。
- Owner/Admin 添加、编辑、打标签、重新授权和删除账号。添加时粘贴 Cursor 网页 Cookie，服务端验证身份并申请桌面会话；Cookie 输入在请求完成或弹窗关闭后清空。
- 在空间设置创建邀请，复制的链接把票据放在 URL fragment 内，服务端和代理不会收到此票据。页面打开后立即从当前历史地址移除票据。已有用户先登录再接受，新用户以受邀邮箱设置密码。
- Member/Viewer 加入后默认看不到任何账号。在具体账号的「账号授权」中逐个分配 view/use；Viewer 只能 view。角色、授权与账号操作始终在后端再次校验。
- 列表默认每 10 秒读取最新快照；顶部刷新按钮提供「立即刷新列表」及 10 秒、30 秒、1 分钟设置，选择保存在当前设备。页面隐藏时暂停，返回时立即更新；列表更新不会批量请求 Cursor。Linux 服务尚无 V2 周期额度查询，单账号「刷新」才请求最新额度。失败保留上次成功数据，并显示时间/陈旧状态。
- 美元上限优先采用当前可解数据，再保留同账号、同套餐、同账期且与当前容量不冲突的历史上限；重新授权同一账号也会保留。跨账号补齐仅使用当前可见、套餐信息相同、账期重叠且符合本账号已知容量的观测。同名套餐有不同容量而本账号无法区分时，相应金额留空，避免新旧额度混用。历史已丢失时，账号管理者可在「编辑资料与标签 → 额度上限参考值」补填本账期已知金额；手动参考值不传播给其他账号，账期或套餐变化后失效，也不改变最后成功查询时间或剩余百分比。
- 明细按需查询 Cursor，沿用 tier 分类、Grok 分支、请求节流、授权代次与短期缓存。空间/用户切换清空旧视图并取消请求，过时响应不更新当前界面。浏览器仅持久化显示偏好，会话在 HttpOnly Cookie 中。
- 空间设置负责邀请、角色、所有权、成员移除、团队删除及空间审计。实例管理员在实例设置启停用户，不自动获得他人个人空间访问权。个人设置提供改密码、会话撤销和独立皮肤/明暗选择。

## 手工切换

具有 use 权限的账号才展示切换按钮。选择 macOS 或 Windows，保存工作并确认后，Web 通过 CSRF 保护的 POST 生成短命令。命令内含最长 5 分钟有效的一次性下载链接；粘贴到终端后才请求本服务，完整下载固定脚本并执行，下载失败不会执行部分脚本。票据绑定当前会话、空间、账号、授权代次及凭证版本；下载时再次验证授权，消费与脚本生成在同一事务中，生成失败回滚。

页面只在弹窗内存中保留短命令，过期、关闭弹窗、切换空间或退出时清除。终端持一次性链接即可 GET 下载，无需复制浏览器 Cookie；链接相当于临时授权，请勿分享，也不要在反向代理访问日志中记录完整下载 URL。下载一次后不可再领，退出登录、会话撤销或账号撤权会阻止尚未发生的下载；已经领取的脚本包含登录凭证，无法通过撤权收回。剪贴板由用户自行清理。API 不提供任意脚本模板或普通裸凭证接口。

macOS 在系统终端粘贴，Windows 在独立 PowerShell 粘贴。脚本正常退出 Cursor、创建包含 WAL 的备份、事务写入并重启，不强杀 Cursor。账号路径/备份恢复边界沿用 [legacy 脚本说明](operations.md)。浏览器无法确认本机切换结果，请在 Cursor 内核对账号。独立桌面默认直接在弹窗内切换，并保留本地终端执行入口，见 [桌面说明](v2-desktop-operations.md)。

## 源码与 wheel

```bash
uv sync --locked
npm --prefix frontend ci
uv run --frozen python dev/build-web.py
# 按 API 文档创建独立密钥并执行 server-init。
export CURSOR_CORE_MODE=server
export CURSOR_CORE_DATA_DIR=/你的路径/v2-data
export CURSOR_CORE_KEY_FILE=/你的独立路径/master.json
uv run --frozen cursor-api --public-origin http://127.0.0.1:8000
```

构建后的 Web 复制到包内 `web_v2/`（不纳入源码 Git），再 `uv build --wheel` 即得到包含界面的 wheel。直接构建未生成前端的 wheel 仍可提供 API，不包含网页。容器 Dockerfile 会自动构建与装入 Web。

前端热更新：API 使用 `--public-origin http://127.0.0.1:1420`，仍监听 8000，另运行 `npm --prefix frontend run dev`。Vite 将 `/api/v1` 同源代理到 8000；此方式仅用于本机开发。

## 远程 CLI

```bash
cursor-remote --server https://panel.example.com --login owner@example.com workspaces
cursor-remote --server https://panel.example.com --login owner@example.com list
cursor-remote --server https://panel.example.com --login owner@example.com \
  list --workspace 空间UUID --query 名称 --tag 开发 --limit 50 --offset 0
cursor-remote --server https://panel.example.com --login owner@example.com \
  detail --workspace 空间UUID --account 账号UUID
cursor-remote --server https://panel.example.com --login owner@example.com \
  refresh --workspace 空间UUID --account 账号UUID
```

每次执行隐藏输入密码，检查 API 主版本，使用同一登录/CSRF/授权流程；省略空间时使用本人个人空间。会话仅保存在进程内存，命令结束主动撤销，失败时提示从个人设置撤销。拒绝不安全的外网 HTTP origin 和重定向，不提供密码参数或明文会话文件。P5 桌面已有独立设备会话；远程 CLI 仍采用每次登录后撤销的短会话。

`cursor-core` 继续作为离线维护入口，持有数据目录锁；常规远程查询应使用 `cursor-remote`，不要用运维 Actor 参数代替用户认证。

## 页面风格与显示偏好

账号页沿用 V1 的紧凑卡片网格、状态角标、续期圆环和燃尽水印；默认使用液态玻璃皮肤与深色模式。左下角「显示偏好」集中提供主题、明暗模式和卡片显示项。经典、液态玻璃可直接切换，悬停或点击右侧箭头可选择赛博朋克、石墨极简、青野绿意、工程蓝图；选中后，面板与侧栏入口显示当前主题名。主题与明暗独立，并保留浏览器已保存的偏好；个人设置使用相同的六种主题名称。

侧栏按工作空间、设置、底部工具分组，支持收起为图标栏。账号页下方最多显示「全部标签」和三个快捷标签；更多标签通过可搜索的弹层选择，只有弹层列表滚动。选中未展示的标签会替换第三个快捷位，不增加菜单行数。帮助与文档入口提供可重复阅读的 6 步指引和分模块使用文档。可用高度低于 840px 时改为一行当前标签入口，低于 680px 时将设置合并为弹出入口，低于 520px 时压缩间距并将帮助入口收为底部图标；手机导航独立展开，不由标签数量撑高。返回账号页保留当前筛选，切换用户、空间或实例时清空标签状态。标签目录来自当前空间内全部可见账号；筛选请求额外读取一页只读目录，避免筛选后其他标签消失。底部载入时间表示列表读取时间，不代表上游额度刷新时间。

侧栏使用确认设计稿的局部主题令牌（`sidebar-tokens.css`），与空间选择、标签及偏好弹层共享颜色、边框和选中态。图标统一使用 Lucide Vue，保持相同画布和描边。空间切换是包含图标、名称、身份说明及双箭头的完整按钮；底部保留整行「收起侧栏」，右侧用户图标可查看当前身份或退出登录。Lucide 与其 Feather 来源图标的许可全文随前端发布在 `licenses/lucide.txt`；更新说明使用的 Markdown 解析器及其浏览器依赖许可随前端发布在 `licenses/markdown-it.txt`。

三条额度条分别显示 Cursor Models、Other Models 和综合池：条长表示已用百分比，右侧显示剩余百分比；美元上限沿用后端推算结果，缺失时不显示上限。侧栏「卡片显示项」只控制展示，不改变查询范围。搜索框支持 `/` 聚焦；名称和额度排序作用于当前页，标签筛选作用于整个可见账号集合。

排序、账号页标签、空间切换和权限等下拉框使用 Vue 公共组件 `UiSelect`，沿用 V1 的菜单材质、悬停高亮与选中勾选。菜单随皮肤和明暗变化，空间不足时向上展开；支持方向键、Home/End、输入文字定位、Enter/空格确认，Esc 或点击外部收起，Tab 移到下一控件。侧栏的标签、主题、协作和用户入口使用顶层 `UiPopover`，根据窗口边缘调整位置。标签弹层支持搜索、方向键选择及单条结果 Enter 确认；Esc 逐层关闭并恢复焦点。弹窗内按 Esc 先收起下拉，再按一次关闭弹窗。

## 模拟预览与验证

```bash
npm --prefix frontend ci
npm --prefix frontend run build
uv run --frozen python dev/preview-v2.py --port 18763
```

预览仅监听 loopback，每次使用临时数据库与临时密钥，退出自动删除；所有 Cursor 请求由模拟网关处理。登录邮箱 `owner@example.test`、`member@example.test`、`viewer@example.test`，密码均为 `Preview password 42!`。Owner 有个人和团队账号，Member 只有一个团队账号的 use，Viewer 初始无授权。预览生成的脚本在访问本机 Cursor 前停止。

```bash
uv run --frozen python dev/export-openapi.py
npm --prefix frontend run api:generate
npm --prefix frontend run build
npx --prefix frontend playwright install chromium
npm --prefix frontend run test:e2e
uv run --frozen python -m unittest discover -s tests -v
docker build -t cursor-panel-v2:p3 .
uv run --frozen python dev/verify-v2-container.py cursor-panel-v2:p3
```

生成的 OpenAPI 和 TypeScript 类型纳入 Git，CI 重新生成并检查漂移。浏览器脚本自动启动临时预览，覆盖账号操作、双用户隔离、单账号授权、邀请、撤销会话、弹窗焦点、过时请求和移动布局。容器验证只创建独立临时卷/容器，覆盖空环境初始化、持久化、备份与新环境恢复后清理；不读取真实数据。
