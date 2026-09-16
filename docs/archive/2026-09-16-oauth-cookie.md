# Issue #2：OAuth Cookie 导入

基线：`0c1207f`。关联 [Issue #2](https://github.com/devilcoolyue/cursor-panel/issues/2)。

Cookie 的 `user_…` 与 JWT 的 `google-oauth2|user_…` 指向相同 ID 时，旧入口因只允许 `auth0|` 而在出站前拒绝；V2 又将此校验错误转成 502，页面提示网络失败。

修复仅在 Cookie ID 比较处接受单个非空提供方前缀，保留 JWT 的完整 subject 用于网页身份验证、桌面凭证检查、持久化与续期。空前缀、多层前缀、格式错误和不同用户 ID 继续被拒绝；不同 OAuth 提供方即使后缀与邮箱相同，也不能覆盖已有授权。

新增 `CookieSessionError` 区分本地 Cookie 输入错误与上游失败。添加/重新授权接口对前者返回 422 和固定错误码，前端只按白名单选择文案，不显示任意响应正文；上游网络及桌面凭证错误仍返回脱敏 502。OpenAPI 与生成类型同步更新。

验证结果：

- 定向回归 10 项通过，覆盖裸 ID / Auth0 / Google / GitHub 格式、编码 Value、错误 subject、导入、重新授权、强制续期及原凭证保留。
- 全量 Python 回归 372 项：362 通过，10 项因本机缺少 PowerShell 跳过。
- Web 和独立桌面 Chromium 流程通过，实际提交错误 Cookie、过期 Cookie、账号不匹配 Cookie，随后成功添加及重新授权 Google 格式的模拟账号；Web 额外验证未知错误码及任意正文不会直接显示。
- 桌面后台契约 8 项通过；前端类型/生产构建、API 契约生成、版本一致性和 `git diff --check` 通过。
- 修改文件的 Ruff E4/E7/E9/F 检查仅有 `tests/test_v2_desktop.py` 原有的 6 条告警（1 条 F811、5 条 E702），与基线核对一致；本次新增代码无告警。

所有测试使用合成 Cookie、临时数据库和模拟网关。未验证真实 Google/GitHub OAuth 会话，也未制作 Windows 安装包；此记录不扩大平台及上游支持声明。桌面连接远程实例时现有桥只保留错误状态码，因此无效 Cookie 显示通用 422 提示，具体错误文案用于 Web 与独立本地桌面。

## 真实导入补充验证

v0.0.7 发布后，用户报告 Google 账号仍无法导入，并明确授权使用其提供的 Cookie 在本地复现。诊断在临时数据库及内存密钥组成的隔离 Core 中执行，未操作已安装面板数据库或本机 Cursor，未把真实凭证写入源码或诊断日志。

实际失败点在网页身份校验：Cookie 解析已通过，`GET /api/auth/me` 返回 200，但响应的 `sub` 是裸 `user_…`，与 JWT 的 `google-oauth2|user_…` 具有相同 ID。旧的 `verify_identity` 只移除 `auth0|`，因此再次抛出身份不一致并映射为 502。v0.0.7 的模拟网关在网页和桌面身份两端均返回完整前缀，漏掉了此差异。

补充修复允许上游身份资料的裸 ID 匹配完整 OAuth subject，保留 JWT 和持久化的完整提供方身份。两个显式不同的提供方、不同用户 ID、错误邮箱仍被拒绝。Web/桌面模拟网关改为网页仅返回裸 `sub`、桌面返回完整 `authId`，使原有导入/重新授权/续期回归覆盖真实差异。

同一 Cookie 在修复后的真实导入结果：网页 `me`、桌面 callback/poll、桌面 `GetMe` 和 6 个额度接口全部返回 200；签发桌面 Token 为 `type=session`，其 subject 与网页 JWT 完全一致；账号及额度快照成功保存。临时库在完成后删除，密钥仅存于已退出进程内存，日志只保留接口状态和身份相等性，不含邮箱、用户 ID 或令牌。这只确认本次 Google 账号导入，不等同于真实 GitHub、原生切换或多设备会话验证。

补充修复本地回归：374 项 Python 测试中 364 通过，10 项因缺少 PowerShell 跳过；Web/独立桌面浏览器完整流程通过，使用上述已修正的模拟网关验证 OAuth 添加和重新授权；修改代码的 Ruff E4/E7/E9/F 与 `git diff --check` 通过。
