# 实现与维护

[返回 README](../README.md) · [部署与配置](operations.md) · [阶段归档](archive/2026-09-07.md)

## 模块与入口

| 文件 | 职责 |
|---|---|
| [client.py](../cursor_dashboard/client.py) | 网页授权、桌面 RPC/套餐请求、错误分类与重试；每次尝试新建并关闭 Requests Session |
| [sessions.py](../cursor_dashboard/sessions.py) | Cookie 交换、身份检查、旧账号迁移、桌面凭证续期 |
| [desktop.py](../cursor_dashboard/desktop.py) | PKCE、Token 格式/时间检查、命令生成；不在服务器执行命令 |
| [switch_links.py](../cursor_dashboard/switch_links.py) | 有界内存短链接、原子单次领取和下载命令包装 |
| [scripts/](../cursor_dashboard/scripts/) | macOS/PowerShell 包装脚本与公共 SQLite 切换引擎 |
| [usage.py](../cursor_dashboard/usage.py) | 桌面响应适配、额度池反解、卡片与模型明细组装；另保留旧 `collect()` 网络入口 |
| [pools.py](../cursor_dashboard/pools.py) | 保留同账期历史，匹配套餐、账期与已知容量后补齐缺失上限 |
| [snapshot.py](../cursor_dashboard/snapshot.py) | 进程内快照、同账号刷新锁、最后成功数据及失败状态 |
| [scheduler.py](../cursor_dashboard/scheduler.py) | 逐账号后台刷新、空闲降速、限流退避 |
| [store.py](../cursor_dashboard/store.py) | SQLite 事务、账号/快照持久化、一次性 JSON 迁移、续期租约和条件更新 |
| [admin.py](../cursor_dashboard/admin.py) | 密码哈希、会话、登录限速、切换策略、只供展示的凭证元数据 |
| [server.py](../cursor_dashboard/server.py) | FastAPI 编排、鉴权、服务端节流、按需明细、模板和静态资源 |
| [cli.py](../cursor_dashboard/cli.py) | `cursor-quota`，按账号/接口串行查询，终端及 JSON 输出 |
| [config.py](../cursor_dashboard/config.py) | 运行配置；管理员密码由 `admin.py` 单独读取 |
| [web/](../cursor_dashboard/web/) | 无构建静态资源；管理片段由服务器注入同一个页面模板 |

`pyproject.toml` 注册 `cursor-panel = cursor_dashboard.server:main` 和 `cursor-quota = cursor_dashboard.cli:main`。当前两个入口使用 `DESKTOP_ENDPOINTS` 和 `assemble_desktop()`；`ENDPOINTS`、网页方法及 `collect()` 仍在代码和包导出中保留，属于旧接口兼容层，不是当前 CLI 主路径。

## 授权与取数

初次授权流程为：解析 Cookie 格式与到期声明，调用网页 `me` 校验邮箱和 subject，经 `desktop_callback` 和 `desktop_poll` 换取桌面凭证，再调用 `desktop_me` 校验身份。Token 的本地解析不验证签名，身份确认依赖远端接口。新增/重新授权还需完成 6 项桌面额度请求，成功后才提交账号和快照。

常规刷新、明细与切换先调用 `ensure_account()`。保存的凭证可用时直接复用；有 RT 时只走桌面续期，无 RT 的旧记录才通过 Cookie 迁移，已经标记撤销的记录直接要求重新授权。

续期窗口为 `expires_at - min(TOKEN_REFRESH_MARGIN, max(30, lifetime * 0.2))`，其中 `lifetime = token_expires_at - auth_refreshed_at`。这是下一次查询时执行的按需逻辑，没有独立的精确定时续期任务。桌面请求遇到 `AuthExpired` 后强制续期并重试一次。

同账号续期通过 SQLite `auth_leases` 串行化：租约 300 秒、每 30 秒续租，等待者最多等待 180 秒。保存时比较数据库 id、授权代次、旧 RT 和 Cookie，避免旧请求覆盖新授权或重建已删账号；标记撤销不更新最近成功续期时间。续期响应缺少独立 `refresh_token` 时，现有适配会使用 `access_token` 作为 RT，不保证两个字段永远不同。

`DESKTOP_ENDPOINTS` 当前包含 6 项，均访问 `https://api2.cursor.sh`：

| 方法 | 上游路径 |
|---|---|
| `desktop_me` | `POST /aiserver.v1.DashboardService/GetMe` |
| `desktop_plan` | `POST /aiserver.v1.DashboardService/GetPlanInfo` |
| `desktop_profile` | `GET /auth/full_stripe_profile` |
| `desktop_period` | `POST /aiserver.v1.DashboardService/GetCurrentPeriodUsage` |
| `desktop_grok` | `POST /aiserver.v1.DashboardService/GetSandUsageStatus` |
| `desktop_limit` | `POST /aiserver.v1.DashboardService/GetHardLimit` |

每次后台只处理一个账号，其 6 项请求通过 `gather(return_exceptions=True)` 并发汇总，使用同一个线程池；不要改成每账号再建线程池。明细另外调用 `GetAggregatedUsageEvents`，不放入定期请求集合。CLI 使用相同桌面数据适配，串行查询；`-c` 分支只使用本次交换出的凭证。

## 刷新、错误与快照

服务端调用先经 `_pace()` 排时槽，再由信号量限制 `fetch_one` 并发。重试在 `fetch_one` 内退避，未再次排外层时槽，因此配置描述针对任务调度，不保证每次实际 HTTP 尝试间隔严格相同。

错误分类：401、403 JSON、指定登录重定向归 `AuthExpired`；403 非 JSON、429、503 归 `RateLimited`；500/502/504、连接错误和超时按配置重试。禁止自动跟随登录跳转。批量结果同时有认证与限流错误时，`_classify()` 优先限流。Grok 的普通异常返回空结果，认证和限流必须继续上抛。

调度器选择 `attempted_at` 最旧的账号，完成查询后等待 `max(REFRESH_MIN_GAP, 目标周期 / 账号数)` 再乘随机抖动。限流将退避倍数翻倍，累计 10 次成功后乘 0.7 收紧；其他错误不清零成功计数，不能描述为严格的连续成功。API 活动更新空闲状态，管理员会话检查也算活动。

快照标识为邮箱，缺失时回退姓名；fingerprint 是 Cookie 短哈希。AT/RT 轮换不改变 fingerprint，Cookie 改变使旧快照无效。失败仅更新错误、次数和尝试时间，不覆盖 `data` 与 `ok_at`；错误类型变化重新计数。已有数据需连续两次认证失败才设 `expired`，从未成功者首次即失效。`stale` 是有数据但最近失败，`pending` 是尚无成功或失败结果。

快照同步写 SQLite，启动时恢复并重建套餐观测。落盘失败保留内存数据，代码会吞掉该存储异常，不能将重启恢复视为持久化成功保证。没有历史快照序列，也没有周期趋势查询。

## 数据口径

- `assemble_desktop()` 将桌面周期、套餐和按量付费字段适配到 `assemble()`。主额度按上游已用百分比计算剩余，`remain()` 下限为 0；部分缺失字段按 0 处理，不能保证自动发现所有上游格式变化。
- 上限反解使用 `T = spend_usd / (total_pct / 100)`、`A/B = (api_pct - total_pct) / (total_pct - auto_pct)`。总百分比为 0 或达到 99.99% 时全部留空；分档触顶或百分比差小于 0.05 个百分点时只保留可解的总池。不要移除保护或写死额度。
- 上限优先使用本次反解，其次保留同账号、同套餐及同账期的历史；本次已知容量与历史冲突时放弃历史。在成功快照写入时保留历史，跨账号估算不写回快照，避免反复传播。
- 同名套餐可能同时有多种容量。补齐先核对套餐信息、重叠账期与本账号已知上限，例如综合池 `$472.50` 不会借用综合池 `$495` 的分池上限。允许至多 0.1%（最低 2 美分）的估算误差；同容量内采用有过半观测支持的中位数，补齐后的两分池之和须与综合池相符。缺少综合上限且观测有多种容量时，只补齐各容量共同认可的档位，其余留空，不按多数账号猜测，也不生成 `$33.75` 一类混合容量。
- `pools.observe()` 按账号保存最近一次有效观测，部分解只贡献已知档位；每次成功刷新替换旧观测，删除账号清理观测，启动重建。观测无独立 TTL，仍受快照新旧程度影响；`plan_pools` 是观测计数。
- 补齐的档位标记 `limit_inferred` 及 `limit_source`（`history` 或 `plan`），不改变上游百分比。CLI 仅使用当前反解，不走历史或跨账号补齐。美元上限仍为估算。
- 卡片消费分母为综合估算池 `quota.overall.limit_usd`。`plan.included_usd` 来自套餐包含金额，不等于该池；`notice` 在 CLI 展示，Web 卡片不显示。
- 明细窗口从快照账单起点到请求时刻；缓存键按账号与 Cookie fingerprint，TTL 默认 60 秒。缺少账单起点返回 409。分类按数值 `tier == 2` 归 Cursor Models，其余归 Other Models；成本用返回的 cents，输入、输出、缓存写、缓存读独立保留，不硬编码模型单价。
- Grok 用量缺失、`includedLimitZero` 为真或 `hasNonZeroIncludedLimit` 明确为假时整行省略；有效 0 用量显示 100% 剩余，不按套餐名或 `hasAvailableUsage` 过滤。重置时间优先 `nextResetTimestampUtc`，缺失才回退到起点加 7 天。

## HTTP 接口

以下 `{id}` 为 `store.account_id()` 返回的邮箱/姓名，调用方需 URL 编码；切换策略中的 `account_ids` 则是稳定的整数数据库 id。鉴权矩阵见[部署文档](operations.md)。

| 方法与路径 | 行为 |
|---|---|
| `GET /api/config` | 口令需求、管理员状态、后台目标周期 |
| `GET /api/status` | 调度器状态和套餐观测计数，周期是目标值 |
| `GET /api/account-index` | 账号身份索引、`can_switch`、部门人数，无上游请求 |
| `GET /api/accounts` | 快照卡片、部门人数与总数，无上游请求 |
| `GET /api/accounts/{id}` | 单卡快照 |
| `POST /api/accounts` | `{cookie, label?, department?}`，授权并保存；同邮箱更新 |
| `PATCH /api/accounts/{id}/department` | `{department}`，只更新部门及修改时间 |
| `DELETE /api/accounts/{id}` | 删除本地账号和快照，不调用 Cursor 撤销会话 |
| `POST /api/accounts/{id}/refresh` | 单卡回源或返回冷却/操作配额提示 |
| `GET /api/accounts/{id}/usage-detail` | 命中缓存或按需获取周期明细 |
| `POST /api/accounts/{id}/switch-command` | 身份验证后返回 macOS/Windows 短命令、完整脚本、凭证到期时间及 `download_expires_at` |
| `GET /api/s/{token}/{platform}` | 用一次性链接领取对应平台的 UTF-8 脚本；不依赖浏览器 Cookie |
| `GET /api/admin/session` | 当前管理员会话状态，登录后包含 CSRF 和到期时间 |
| `POST /api/admin/login` | `{password}`，设置 Cookie 并返回会话元数据 |
| `POST /api/admin/logout` | 删除当前管理员会话 |
| `GET /api/admin/accounts` | `q`、`department`、`page`、`page_size`，本地凭证元数据查询 |
| `GET /api/admin/switch-policy` | 切换策略、账号与部门候选项 |
| `PUT /api/admin/switch-policy` | `{all_accounts, departments, account_ids}`，验证引用后保存 |

账号列表和索引支持 `department`：省略为全部，空串为未分组；部门人数和 `total` 仍按全库返回。管理员分页默认 20，每页最多 100。普通响应由 `public_account_view()` 移除授权时间信息，只附加切换能力；管理员列表也不返回凭证原文。

切换命令使用普通 API 鉴权加开放策略，在出站前及返回前都检查；管理员修改请求还受 CSRF 约束。单卡刷新、未缓存的明细和切换共用令牌桶，默认容量 5、每秒补充 `5/60`。刷新用 notice 表示配额不足，明细和命令生成返回 429。

短链接使用 24 字节随机数（192 位），进程内最多保存 128 组完整脚本。每组 macOS/Windows 共用一个票据，锁内原子领取；未知、已用、过期链接返回 410，错误系统和 HEAD 请求不会消耗有效链接。到期时间取生成后 300 秒与访问凭证到期时间的较早者，发行/领取时清理过期记录，服务另每 30 秒清理一次，退出时清空。下载再次比较数据库记录及凭证版本，检查生成者管理员会话或当前访客策略，权限撤销返回 403。下载响应及错误禁止缓存。`desktop.build_commands()` 保留自包含命令生成能力供离线测试和旧预览使用，线上接口将命令替换为下载包装；完整脚本内容保持一致。

## 前端约束

`/` 与 `/admin` 都通过 `server.render_page()` 返回 `index.html`，并注入 `admin.html` 片段。`#quota` / `#admin` 在同一文档内切换工作区；静态文件不能直接作为完整页面打开。CSS/JS URL 使用模板和资产内容哈希，HTML/静态资源要求缓存重验证，API 响应使用 no-store。

`app.js` 管理额度视图，`admin.js` 暴露 `AdminWorkspace`。普通搜索在当前部门的浏览器数据中过滤，管理员搜索在服务端跨分页查询。列表轮询用代次和 AbortController 防止旧响应覆盖新部门；管理员登录状态变化也使旧请求失效。单卡手动刷新显示骨架，完成后只替换该卡、按排序重排并恢复焦点，后台轮询不触发入场动画。

`SKINS` 声明 6 种皮肤，`data-skin` 与 `data-theme` 独立。`tokens.css` 保存主题令牌，`base.css`/`ui.css`/`admin.css` 使用令牌，`skins/*.css` 放带皮肤前缀的形态差异；添加皮肤需同步令牌、声明、样式及 HTML 引用。默认皮肤还要同步 HTML 样式加载前的内联引导脚本。

`CARD_OPTIONS` 同时生成显示设置与卡片渲染条件；旧偏好与默认值合并，过滤未知键与非布尔值。关闭所有元信息时省略容器，关闭 Grok 显示不减少请求。额度角标和条形颜色使用同一阈值，周期起点未知时不画推测进度。

公共控件来自 `ui.js` / `ui.css`：

```javascript
PanelUI.init(container);                // 动态插入组件后增强
PanelUI.select.refresh(selectElement);  // 代码修改 value 后同步显示
PanelUI.select.focus(selectElement);
PanelUI.open(dialogElement);
PanelUI.close(dialogElement);
await PanelUI.confirm({ title, message, onConfirm });
await PanelUI.alert({ title, message });
```

原生 select 保留值与事件，不支持 Popover API 时回退原生控件。弹窗保留 native dialog 顶层、焦点和 open 属性观察；固定定位、只在 `[open]` 时启用 flex，正文单独滚动。异步确认期间禁止重复提交和关闭，错误在弹窗内显示。用户内容使用转义或 `textContent`。

玻璃动效在 `glass-motion.js`，仅玻璃皮肤且未启用减少动态效果时开启；缺少 Web Animations API 时停用。保留卡片的 tooltip 层叠：不在卡片上随意添加 overflow 裁剪、hover transform 或每卡背景模糊；周期环不接收指针，操作按钮以 hover / focus-visible 显示。水滴形变、缓动与数值细节以代码为准，不在说明中维护第二份参数表。

## 验证入口

```bash
uv sync --locked
uv run --frozen python -m unittest discover -s tests -v
node --check cursor_dashboard/web/js/app.js
node --check cursor_dashboard/web/js/admin.js
node --check cursor_dashboard/web/js/ui.js
node --check cursor_dashboard/web/js/glass-motion.js
```

测试使用标准库 unittest、模拟网络和临时数据库，无需真实账号。`test_desktop.py` 的 SQLite 引擎集成测试需要带 `node:sqlite` 的 Node 22.13+；缺失时跳过。这验证公共 SQL 引擎，不等于执行真实 Cursor 的退出、重启或 Windows PowerShell。

| 测试文件 | 验证范围 |
|---|---|
| `test_client.py` | 认证/限流分类、重试、Grok 错误分支 |
| `test_sessions.py` | 迁移、轮换、并发续期、重新授权竞争、失败保留 |
| `test_desktop.py` | Token 校验、命令编码、权限入口、备份、WAL 与事务回滚 |
| `test_usage.py`、`test_pools.py` | 桌面字段适配、上限估算、明细分类、Grok 资格、同套餐补齐 |
| `test_snapshot.py`、`test_scheduler.py` | 失败保留、失效确认、重启、调度与退避 |
| `test_store.py` | 迁移、去重、部门、并发写入 |
| `test_server.py` | 资源版本、静态缓存、出站并发/时槽、快照入口 |
| `test_admin_core.py`、`test_admin_http.py` | 会话、登录限速、CSRF、授权边界、分页和策略变更 |

当前可用的综合页面预览：

```bash
uv run --frozen python dev/preview-admin.py --port 8792
```

打开 `http://127.0.0.1:8792/admin`，密码为 `preview-admin-password`。该进程在导入配置前设置临时 SQLite 路径，关闭调度并替换出站函数；内含 27 个模拟账号。可以验证登录、凭证分页、权限和额度视图；只模拟 `desktop_me`，新增授权、真实续期、额度刷新和明细不属于它的成功流程。预览切换命令在接触本机 Cursor 前退出。

已安装 Playwright CLI 时，在独立浏览器会话执行：

```bash
playwright-cli --session archive-check open http://127.0.0.1:8792/admin
playwright-cli --session archive-check run-code --filename dev/verify-admin.js
playwright-cli --session archive-check close
```

也可用 `npx --yes --package @playwright/cli playwright-cli` 替代命令名。脚本固定使用 8792，端口被占用时需同时调整预览端口和验证脚本中的 URL，不要对已有生产服务执行验证。截图输出到 `output/playwright/`，目录需预先存在；验证会修改模拟库中的开放策略。

### 旧预览与专项实验

`dev/preview.py` 保留早期 18 账号动效夹具，当前未注入 `__ADMIN_CONTENT__`，管理脚本初始化失败，不能作为当前版本可用预览。依赖该夹具的 `verify-glass.js`、`verify-detail-motion.js`（8789）和 `verify-switch.js`（8790）保留待适配；不得将脚本存在描述为当前回归已通过。`preview-admin.py` 仍复用其中的模拟数据函数，不可直接删除文件。

`dev/verify-session-lifecycle.py` 是真实双账号会话实验：`prepare --experiment EMAIL --control EMAIL` 从所选账号库读取 Cookie，签发独立桌面会话并验证；手动网页退出后，`check --state PATH` 继续验证保存的桌面凭证。它会访问 Cursor 并在私有临时目录保存 Cookie、AT/RT 和报告，不修改本机 Cursor 文件，不能作为离线测试运行。执行前明确实验账号、库路径和远端会话副作用，结束后妥善处理敏感状态文件。此次文档归档未重跑该实验。
