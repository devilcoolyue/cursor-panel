# 部署与运行

[返回 README](../README.md) · [实现与维护](maintenance.md) · [阶段归档](archive/2026-09-07.md)

## 启动与部署

运行依赖为 Python 3.10+、FastAPI、Uvicorn、Requests；版本约束在 [pyproject.toml](../pyproject.toml)，解析结果在 `uv.lock`。在项目根目录执行 `uv sync --locked` 安装锁定依赖。

`cursor-panel` 参数：`--host` 默认 `127.0.0.1`，`--port` 默认 `8787`，`--no-open` 禁止自动打开浏览器。仅本机 host 会自动开浏览器。

以下示例将库放到项目 `data/`，仅首次未初始化时才读取其中的旧 JSON；目录必须事先存在且运行用户可写：

```bash
mkdir -p data
chmod 700 data
export DATABASE_PATH="$PWD/data/accounts.db"
export ACCOUNTS_PATH="$PWD/data/accounts.json"
export PANEL_TOKEN="$(openssl rand -hex 24)"
uv run --frozen cursor-panel --host 0.0.0.0 --port 8787 --no-open
```

生产环境应将口令保存在受保护的服务配置中，避免每次重启随机改变口令。应用本身不终结 TLS；通过 HTTPS 反向代理访问时，需正确传递 Host 和协议，并只信任实际代理的转发头，确保 Origin 检查及管理员 Cookie 的 Secure 属性与外部地址一致。

按单进程部署。临时切换链接、快照、套餐观测表、出站节奏和后台调度器在进程内，多个 worker 或实例不会同步这些状态；数据库续期锁只协调凭证轮换。CLI 与面板也没有跨进程共享限速器，不宜同时批量查询。

切换下载链接使用请求中的 Host、协议及 ASGI `root_path` 生成。反向代理须保留外部 Host 并正确传递 HTTPS 协议；若使用子路径挂载，需配置对应 `root_path`（现有前端 API 路径仍假设部署在站点根目录）。终端必须能直接访问该地址。下载接口 `/api/s/` 以随机链接本身授权，无需浏览器 Cookie 或面板口令；外层代理认证也需允许该路径访问，并对其禁用缓存及访问日志或脱敏 URL，避免记录临时凭证。链接最多保留 5 分钟，进程重启即失效。

### systemd 模板

[cursor-dashboard.service](../deploy/cursor-dashboard.service) 是待按环境调整的样例，当前仍带 `/data/app/cursor-dashboard` 路径、`cursorpanel` 用户和固定内网监听地址。安装前核对以下项目：

1. 创建运行用户、部署目录和可写的 `data/`，通过 `uv sync --locked` 生成 `.venv/bin/cursor-panel`。
2. 调整 `User`、`Group`、`WorkingDirectory`、`ExecStart`、数据库路径及 `ReadWritePaths`。
3. 设置 `PANEL_TOKEN`；按需设置 `ADMIN_PASSWORD`。不要将实际密码提交回样例。
4. 安装为 `/etc/systemd/system/cursor-dashboard.service` 后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cursor-dashboard
sudo systemctl status cursor-dashboard
sudo journalctl -u cursor-dashboard -n 50 --no-pager
```

首次随机管理员密码会进入服务启动日志，应限制日志访问。模板的 `ProtectHome=true` 不适用于依赖用户 home 目录的安装布局。

## 环境变量

除 `ADMIN_PASSWORD` 在 [admin.py](../cursor_dashboard/admin.py) 初始化时读取外，其余见 [config.py](../cursor_dashboard/config.py)，在模块导入时读取，修改后重启服务。时间单位均为秒。

| 变量 | 默认值 | 含义与边界 |
|---|---|---|
| `PANEL_TOKEN` | 空 | 普通 API 的共享口令；为空时普通 API 开放，管理员仍需登录 |
| `ADMIN_PASSWORD` | 首次随机生成 | 设置/重置管理员密码；未设置沿用库中哈希，值不变不撤销已有会话 |
| `ACCOUNTS_PATH` | 工作目录下 `accounts.json` | 首次迁移来源；也决定未显式设置时的数据库路径 |
| `DATABASE_PATH` | `ACCOUNTS_PATH` 后缀改为 `.db` | 默认即工作目录下 `accounts.db`；父目录不会自动创建 |
| `MAX_WORKERS` | 48 | 服务端通用线程池容量，不等于出站并发 |
| `REQUEST_CONCURRENCY` | 3 | 服务端 `fetch_one` 调用并发上限，最小 1 |
| `REQUEST_MIN_INTERVAL` | 0.5 | 服务端出站任务排队间隔，另加最多 20% 抖动；CLI 每次调用前等待此值 |
| `REQUEST_RETRIES` | 2 | 连接错误、超时及 500/502/504 的重试次数 |
| `RETRY_BASE_DELAY` | 0.25 | 普通错误指数退避基数，另加随机抖动 |
| `RATE_LIMIT_RETRIES` | 3 | 临时限流重试次数 |
| `RATE_LIMIT_BASE_DELAY` | 2.0 | 限流指数退避基数；数字形式的 `Retry-After` 优先，上限 120 秒 |
| `REFRESH_ENABLED` | 1 | `0`、`false`、`no` 关闭后台调度，值区分大小写；手动操作与 CLI 不受此开关控制 |
| `REFRESH_INTERVAL` | 900 | 目标整轮周期，代码最小 60；实际周期还包括取数耗时、抖动、空闲及退避 |
| `REFRESH_MIN_GAP` | 2.0 | 账号间等待的下限基数，之后仍乘 0.85 至 1.15 的抖动 |
| `REFRESH_IDLE_AFTER` | 1800 | 没有 API 活动达到此时长后降速 |
| `REFRESH_IDLE_FACTOR` | 4 | 空闲周期倍数，最小 1 |
| `REFRESH_MAX_BACKOFF` | 8 | 限流退避倍数上限，最小 1 |
| `TOKEN_REFRESH_MARGIN` | 86400 | 续期提前量上限，配置最小 30；短期凭证使用寿命的 20%，且至少提前 30 秒 |
| `MANUAL_COOLDOWN` | 60 | 单卡成功刷新冷却；也决定共享操作令牌桶的补充速率 |
| `MANUAL_BURST` | 5 | 刷新、未命中缓存的明细及切换命令共用的桶容量，最小 1 |
| `DETAIL_TTL` | 60 | 服务端模型明细缓存寿命；与 HTTP 响应的禁止缓存是不同层次 |

常规桌面额度查询每账号 6 个接口，估算基础量可用 `账号数 × 6 / REFRESH_INTERVAL`，不含授权、续期、手动操作、重试及调度等待。例如 42 个账号、900 秒约为 0.28 次/秒的基础量级，不是实测吞吐或限速保证。`fetch_one` 内部重试不会重新申请外层时槽。

保留默认刷新周期作为起点，遇到限流优先延长周期。首次空库会逐个填充快照，没有刷新全部入口。关闭后台后，仅打开页面不会填充或更新额度快照。

## 访问与管理员

| 请求范围 | 鉴权 |
|---|---|
| `/`、`/admin`、`/static/*` | 页面及资源公开，不含账号数据 |
| `GET /api/config`、`GET /api/admin/session` | 公开，返回配置提示或当前会话状态 |
| `POST /api/admin/login` | 管理员密码，失败尝试有持久化限速 |
| 其余 `/api/admin/*` | 有效管理员 Cookie；修改请求还需 `X-Admin-CSRF` |
| 普通账号和状态 API | 有效管理员会话，或在配置口令时提供 `X-Panel-Token` |
| `POST /api/accounts/{id}/switch-command` | 普通 API 鉴权后，再检查管理员身份或切换开放范围 |
| `GET /api/s/{token}/{platform}` | 一次性随机链接授权；重新检查账号凭证、生成者管理员会话或当前访客开放范围 |

有管理员会话的所有 API 修改请求都检查 CSRF，登录接口除外；修改请求还检查 Origin 和跨站标记。所有 API 响应禁止缓存。页面将面板口令保存到 localStorage；管理员凭据使用 HttpOnly、SameSite=Strict Cookie，服务端识别 HTTPS 时设置 Secure。

管理员会话绝对有效期为 12 小时，退出后立即失效。忘记密码时设置新的 `ADMIN_PASSWORD` 并重启；密码改变会清除管理员会话和失败尝试记录。15 分钟窗口内按客户端累计 5 次、全局累计 100 次失败后限速。

凭证列表只读本地库，点击其刷新按钮不会触发 Cursor 续期。AT/RT 声明到期时间来自 JWT 的 `exp`，仅用于展示；无法解析时显示未提供，不代表远端保证可在该时间前续期。进入续期窗口后，仍需等下一次后台或按需查询才实际续期。

普通 API 不是只读入口：持有共享口令者可修改账号与部门。切换开放是额外的命令生成检查，不是普通访客身份或数据隔离。远端会话撤销、客户端退出和已复制命令的边界见 [README](../README.md)。

## 数据与迁移

SQLite 启用 WAL，账号表 schema 标记为 v4，启动时按缺失列补齐。主要表为 `accounts`、`snapshots`、`metadata`、`auth_leases`；管理员初始化另建 `admin_sessions` 和 `admin_login_attempts`。

AT、RT 及旧账号残留的 Cookie 在账号表中明文保存（浏览器授权的账号只存不含凭证的授权标识）；快照只存组装后的业务数据和该列的 SHA-256 前 16 位。初始化时将存在的数据库及 WAL/SHM 文件设为 0600，服务部署仍应使用受限目录和 `UMask=0077`。备份同样包含凭证。

`metadata.legacy_json_migrated` 不存在时读取旧 JSON 并写入迁移标记，即使当时没有旧文件也会标记完成。原 JSON 保留，后续不再自动读取；因此应在首次初始化前放好迁移文件，不要依靠反复改 JSON 更新已初始化的库。

[accounts.example.json](../accounts.example.json) 是旧格式示例，不是运行所需配置。推荐每条提供 `label`、`cookie`，`department` 可选，已有 `email` 可保留以便去重；迁移器实际只导入含非空 Cookie 的对象，缺少姓名时会生成默认值。仅有 Cookie 的账号无法再换取桌面凭证，导入后需在浏览器中重新授权。

`cursor-quota` 默认直接查询共享库中的账号，并可能保存迁移/续期结果；它不写 Web 快照，也不使用面板的同套餐额度池补齐。`cursor-quota -c other.json` 每次临时授权，不读写账号库，凭证仅用于该次进程。

### 备份与恢复

应用没有内置备份、恢复或历史趋势功能。快照只保留每个账号最后一次成功数据和最近失败状态。

备份使用 SQLite 在线备份 API 或 `sqlite3` 的 `.backup`，不要在服务写入时只复制 `.db` 而遗漏 WAL。例如服务器装有 `sqlite3` 时，可按实际路径执行，输出文件名应选新的私有备份名：

```bash
umask 077
sqlite3 /path/to/accounts.db ".backup '/private/backups/accounts-stage-20260907.db'"
sqlite3 /private/backups/accounts-stage-20260907.db 'PRAGMA integrity_check;'
```

恢复前停止所有使用该库的面板和 CLI，保留现库备份，通过 SQLite 恢复操作还原并检查完整性、属主和权限。不要混用不同备份的主库与 WAL/SHM。库中包含管理员密码哈希、会话和开放策略，恢复后也需核对这些设置。Cursor 客户端的 `state.vscdb` 切换备份与服务器账号库是两类不同数据。

## 排障

| 现象 | 检查或处理 |
|---|---|
| 库打不开 | 父目录、运行用户权限、`DATABASE_PATH`；不能与 `ACCOUNTS_PATH` 指向同一文件 |
| 新部署一直等待更新 | 后台是否启用、最后统计时间；`/api/status` 查看 enabled、idle、backoff、throttled |
| 卡片数据陈旧 | 区分限流、网络和认证错误；重启不会清空持久化失败状态，也不会重算旧快照 |
| 额度上限缺失 | `/api/status` 的 `plan_pools` 是否有同套餐有效观测；估算条件不足时留空属于预期 |
| 明细返回 409 | 快照尚无账单周期起点，等待账号成功刷新 |
| 短链接返回 410 或下载失败 | 链接已使用、过期、服务重启或授权已变更时重新生成；检查终端能否访问面板地址及反向代理配置 |
| 切换返回 403 | 管理员会话或开放范围；已登录时还应检查 CSRF、Origin 和代理协议 |
| 续期或授权失败 | 确认远端会话是否撤销；临时限流应等待；授权时确认浏览器登录的是目标账号 |
| 本地 HTTP 请求走了代理 | 使用 `curl --noproxy '*'`，不假设所有机器都配置了相同代理 |

不要将真实 `accounts.json`、数据库、会话文件、切换命令或其备份纳入源码归档。`.gitignore` 已排除常见路径，归档前仍需核对实际打包范围。
