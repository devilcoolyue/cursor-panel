# V2 身份与 API 运行

P5 新增设备授权与会话接口：浏览器通过已登录且带 CSRF 的 `POST /api/v1/auth/devices/authorize` 审批，原生后台以 S256 verifier 调用 `POST /api/v1/auth/devices/exchange` 单次交换；`GET /api/v1/auth/devices` 和 `DELETE /api/v1/auth/devices/{session_id}` 查看/撤销设备。设备 API 使用独立类型的 Bearer 会话，拒绝网页 Cookie 或浏览器 Origin/Fetch 请求头，不能复用旧 `PANEL_TOKEN`。生产 `remote_switch=false`，结构化切换签发及领取拒绝；详情见 [桌面连接说明](v2-connected-operations.md)。当前安全与容量维护版本需停止旧进程并升级到 schema `0004_retention`；审计保留与可信代理配置见 [Web 部署](v2-web-operations.md)。

P3 已接入 Vue 界面、Web 手工脚本、容器与远程 CLI，见 [Web 部署与使用](v2-web-operations.md)。首次管理员初始化继续使用离线命令。
P2 在 P1 核心上增加真实用户认证、团队成员、账号授权、审计及独立 `/api/v1`。入口是 `cursor-api`；旧 `cursor-panel` 保留原兼容服务。P3 在此基础上交付新版 Web 界面、容器和手工切换适配。

## 新实例

```bash
uv sync --locked
mkdir -p ./v2-secrets
chmod 700 ./v2-secrets
uv run --frozen cursor-core --key-file ./v2-secrets/master.json keygen
uv run --frozen cursor-core --data-dir ./v2-data --key-file ./v2-secrets/master.json \
  server-init --login owner@example.test
```

`server-init` 在终端隐藏输入两次密码，要求 12–256 字符，不接受命令行密码。它创建实例管理员及个人空间，不创建团队或公开注册入口。不要把密码、会话、Cookie、邀请票据放入 URL、命令参数或截图。

仅本机调试：

```bash
export CURSOR_CORE_MODE=server
export CURSOR_CORE_DATA_DIR="$PWD/v2-data"
export CURSOR_CORE_KEY_FILE="$PWD/v2-secrets/master.json"
uv run --frozen cursor-api --host 127.0.0.1 --port 8000 --public-origin http://127.0.0.1:8000
```

默认监听 `127.0.0.1`。外部访问使用 HTTPS 反向代理，设置精确 `--public-origin https://panel.example.com` 并保留 Host。只有明确 HTTPS origin 才允许 `--host 0.0.0.0`；生产应将后台端口限制在代理可达范围。`public-origin` 不带路径或末尾斜杠。不要增加 worker、启动第二个业务进程或删除锁文件；运维命令需要先停止 API。

应用不信任代理转发的客户端 IP；登录来源限额以直连地址计算，代理后的用户共享其来源限额。HTTP 访问日志默认关闭，请勿在代理中记录请求体、Cookie 或带票据的查询串。

## 已有 P1 实例

先停止核心命令和其他写入者，备份 SQLite 一致性副本及匹配的独立密钥，再执行：

```bash
uv run --frozen cursor-core --data-dir ./v2-data --key-file ./v2-secrets/master.json upgrade
uv run --frozen cursor-core --data-dir ./v2-data --key-file ./v2-secrets/master.json \
  server-init --login 原P1空间Owner的登录邮箱
```

`0001_core → 0002_identity → 0003_devices → 0004_retention` 保留账号、密文、原成员与导入回执，增加会话、邀请、切换票据、审计和设备授权码；设备会话新增类型及标识，旧网页会话与票据继续保留。最后一步仅增加审计索引；保留期清理由运行时维护任务执行。不会因升级自动授予实例管理员或公开数据。首次初始化保留同登录标识的旧用户 UUID/空间，并补建个人空间；再次初始化拒绝。原密钥继续使用，不重新 keygen。

其他 P1 无密码身份及忘记密码的恢复使用离线命令：

```bash
uv run --frozen cursor-core --data-dir ./v2-data --key-file ./v2-secrets/master.json \
  recover-password --login user@example.test
```

此命令记录运维恢复事件并撤销该用户全部会话和未领取票据，不更改其角色或启停状态。持有数据库与密钥的运维者属于信任边界，详见[备份与恢复](core-operations.md)。

## API 客户端约定

1. `GET /api/v1/bootstrap` 读取模式、API 主版本与支持能力。
2. `POST /api/v1/auth/login` 提交 `{login, password}`，保存 HttpOnly 会话 Cookie，并取得 `csrf_token`。登录必须带匹配的 `Origin`。
3. `GET /api/v1/me` 返回用户、个人/团队空间及空间 capabilities。重载页面可通过 `GET /api/v1/auth/csrf` 重新取得当前会话的 CSRF 值。
4. 后续写请求同时发送 Cookie、匹配 `Origin` 和 `X-CSRF-Token`。请求体不接受操作者 ID、角色覆盖、空间覆盖或未知字段。浏览器持久化只存显示偏好，不保存票据或凭证。
5. 收到 401 清理当前身份与查询缓存，重新登录；403 表示可见资源上的禁用动作，404 同时用于不存在与不可见资源，429 表示限速。

API 不开启跨域请求。响应使用 `Cache-Control: no-store`。错误不回显密码、Cookie 或 Pydantic 原始输入；请求体最大 64 KiB，账号分页最大 200。

| 路径（均以 `/api/v1` 开头） | 方法与用途 |
| --- | --- |
| `/auth/login`、`/auth/logout` | POST 登录/退出 |
| `/auth/csrf`、`/auth/sessions` | GET CSRF / 有效会话列表 |
| `/auth/sessions/{session_id}` | DELETE 自己的会话 |
| `/auth/password` | PUT 当前密码与新密码，撤销全部会话 |
| `/me` | GET 当前身份、空间、权限 |
| `/workspaces` | POST 创建团队；个人空间随用户建立 |
| `/workspaces/{w}`、`/workspaces/{w}/owner` | DELETE 团队、PUT 转移 Owner |
| `/workspaces/{w}/members` | GET 成员（Owner/Admin） |
| `/workspaces/{w}/members/{user_id}` | PUT 角色 / DELETE 移除或自己退出 |
| `/workspaces/{w}/invitations` | POST 创建 / GET 有效邀请；原票据只在创建时返回 |
| `/workspaces/{w}/invitations/{id}` | DELETE 撤销邀请 |
| `/invitations/accept` | POST 票据；新用户提供密码，已有用户须先登录 |
| `/workspaces/{w}/accounts` | GET 可见账号、统计及标签计数；POST 旧 Cookie 授权已停用，返回 410 |
| `/workspaces/{w}/accounts/{a}` | GET / PATCH label、tags / DELETE |
| `/workspaces/{w}/authorizations` | POST `{label?, tags?, account_id?}` 开始浏览器授权，返回 Cursor 确认链接；带 `account_id` 为重新授权 |
| `/workspaces/{w}/authorizations/{id}/poll` | POST 查询一次确认结果：`pending`，或 `completed` 并返回账号；仅发起人可用 |
| `/workspaces/{w}/authorizations/{id}` | DELETE 取消未完成的授权 |
| `/workspaces/{w}/accounts/{a}/refresh`、`…/detail` | POST 手工刷新 / GET 明细 |
| `/workspaces/{w}/accounts/{a}/grants` | GET 账号授权列表 |
| `/workspaces/{w}/accounts/{a}/grants/{user_id}` | PUT `{level: view或use}` / DELETE 收回 |
| `/workspaces/{w}/audit` | GET 空间审计（Owner/Admin） |
| `/instance/users`、`/instance/users/{id}` | GET 用户 / PUT `{active: true或false}`（实例管理员） |
| `/workspaces/{w}/accounts/{a}/manual-switch` | POST 签发当前会话绑定的短期票据 |
| `/manual-switch/consume` | POST `{token, platform: macos或windows}` 原子领取固定脚本，仅限 use |
| `/workspaces/{w}/accounts/{a}/switch-command` | POST `{platform: macos或windows}` 生成终端短命令，仅限 use，要求当前 Web 会话与 CSRF |
| `/switch/{token}/{platform}` | GET 持一次性 Web 票据下载固定脚本，无需浏览器 Cookie；原子消费前复查会话、权限和凭证版本 |
| `/health` | GET 健康检查，仍校验 Host/Origin |
| `/instance/audit` | GET 实例事件，不包含他人空间事件 |

账号列表支持 `q`、`tag`、`offset`、`limit`。查询先限定可见账号，再搜索/统计/分页；未经授权的邮箱、标签和快照不会影响结果。列表只读快照，最新数据依赖手动刷新或明细请求；P3 尚未启用 V2 周期后台调度。

额度上限先使用当前快照能直接反解的结果；刷新触顶或方程退化时，保留本账号同一账期、同一套餐此前成功反解的档位，并随快照持久化。账期或套餐信息变更、重新授权会清除这份历史。其余缺失档位按当前空间内可见同套餐账号的完整观测取中位数补齐；搜索或分页不改变来源范围，撤权或删除来源账号后不再使用其观测。不会跨实例、跨空间借用 V1 的全局观测。

补齐档位标记 `limit_inferred=true`；`limit_source=history` 表示本账号同账期历史，`plan` 表示可见同套餐观测。补齐结果不是官方金额保证，也不会成为新的同套餐观测写回账号。没有有效来源时上限仍为 `null`，前端显示 `—` 并解释原因。

普通邀请角色为 Member/Viewer，只有 Owner 可邀请 Admin；邀请有效期 7 天，只能领取一次。用户加入团队不共享其个人账号。Viewer 不能获 use，Member 的 use 包含 view；管理员权限不包含导出凭证归档。账号 capabilities 中的 switch 表示已具备 use 权限，server bootstrap 的 manual_switch、device_sessions 为 true，remote_switch 为 false；账号 switch capability 仅表示 use 授权，入口还须满足对应运行能力。

Owner 可以删除团队；删除整个导入团队同时删除其导入回执和映射，保留审计。单独删除账号保留导入回执，不会被重复导入恢复。

## 验证

```bash
uv run --frozen python -m unittest discover -s tests -p test_identity.py -v
uv run --frozen python -m unittest discover -s tests -p 'test_v2*.py' -v
python desktop/scripts/legacy-baseline.py
uv build --wheel --out-dir output/p2/wheels
python dev/verify-core-wheel.py output/p2/wheels/cursor_dashboard-0.0.1-py3-none-any.whl
```

测试全部使用临时库、合成凭证和模拟上游。wheel 验证另建环境，从独立目录运行安装后的 CLI 和真实 HTTP 进程，不接触真实 Cursor 客户端或账号。
