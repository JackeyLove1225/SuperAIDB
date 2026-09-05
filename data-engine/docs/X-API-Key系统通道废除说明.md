# X-API-Key 系统通道废除说明（20260903）

> 给后续维护者（含 AI）：本文记录一次**安全架构决策**——管理端
> Management API (:2025) 的 X-API-Key 系统认证通道已彻底废除，
> 任何场景都不要再恢复它。

## 一、原设计是什么

迭代 1（20260804 handover 文档）落地用户认证时引入"mgmt 双模认证"：

- `Authorization: Bearer <token>` → 真实用户身份，角色注入权限策略
- `X-API-Key: <API_KEY>` → 系统级身份（**兼容脚本/测试**），角色=system

`system` 角色在权限策略里**跳过全部角色/用户/自助三层规则**（历史遗留
语义：早期聊天路径无用户上下文，默认 system 全权限）。因此持有
`API_KEY`（config/.env）的任何脚本等效 admin：可读全部表数据
（含 users 表）、绕过所有权限矩阵规则。

## 二、为什么废除

1. **旁门风险**：一个静态密钥 = 永不过期的 admin 等效凭据，且绕过
   用户级/自助规则。前端代理早已剥离该头（confused deputy 防护，
   见 agent-chat-ui/src/app/api/mgmt/[..._path]/route.ts 注释），
   实际消费方只剩测试与调试探针。
2. **方向已定**：MCP 通道（mcp_server.py）早已改为用户绑定模式
   （MCP_USER，未绑定拒起 + fail-closed 禁写）——所有通道绑定
   真实用户是既定演进方向，X-API-Key 是最后一个旁门。
3. **需求可替代**：脚本/测试"需要一条能用的通道"的初衷，用
   测试专用用户账号同样满足，且不破坏权限体系。

## 三、废除后的认证面（现状）

| 通道 | 身份 | 角色约束 |
|---|---|---|
| Bearer token | 真实用户 | 三层规则全走（角色/用户/自助） |
| 签名媒体 sig 参数 | 无身份（限 /api/preview/pdf 等），端点 fail-closed 验签 | 端点内自管 |
| Bearer + 角色判定 | admin 专属端点（用户管理/权限规则/备份/审批中心等） | _require_admin 强制 |

**X-API-Key 请求现在一律 401**（中间件不识别该头）。

## 四、脚本/测试怎么认证（替代方案）

### 1. 跑在真实服务上的探针/脚本

用 `tests/_mgmt_auth.py` 的 `auth_headers()`：从
`MGMT_TEST_USER` / `MGMT_TEST_PASS` 环境变量读测试专用账号，
登录换 Bearer token（进程内缓存）。示例见
`scripts/debug/_perm_matrix_check.py`。

测试专用账号由管理员一次性创建（自助注册端点强制 user 角色，
admin 必须走管理面）：

```
管理员登录 → POST /api/auth/users {"username": "test_bot", "password": <强随机>, "role": "admin"}
```

建议建 `test_bot`（admin）与 `test_reader`（readonly）两个，
密码存本机密码管理器/环境变量，不落仓库。

### 2. TestClient 隔离库测试

自建临时用户库（patch `core.auth._get_db_path`），直接
`register_user(..., "admin") + login_user` 铸 token。
示例见 `tests/test_37_mgmt_api.py` 的 `_isolated_auth()`。

## 五、涉及文件（20260903 改动清单）

- `agent/management/server.py` — 中间件删 X-API-Key 分支（唯一认证通道=Bearer）
- `agent/management/deps.py` — `_require_user` 删 X-API-Key 分支
- `agent/management/routers/{dashboard,unrecognized,preview,permissions,
  isolation,industry,datasources,backup_export,approvals}.py` —
  `_require_admin` 删 X-API-Key 分支
- `tests/test_37_mgmt_api.py` — 改用隔离库 Bearer；新增"任何
  X-API-Key → 401"的**废除回归锁**（旁门不得复活）
- `tests/test_07_industry.py` — 删无效的 X-API-Key 头（本层跑无认证模式）
- `tests/_mgmt_auth.py` — 重写为测试专用用户通道（Bearer）
- `scripts/debug/_perm_matrix_check.py` — 探针改用测试专用账号登录

## 六、保留的相邻概念（勿混淆）

- `API_KEY_ENABLED`：**认证开关**（true=开启用户认证），继续使用
- `core/auth.py: verify_api_key()`：函数保留但管理端已无调用方
  （LangGraph :2024 的 X-Api-Key 是另一套系统，见
  docs/archive/langgraph_auth_memo.md，不受本次影响）
- `MGMT_API_KEY` 环境变量：已废弃，不再被任何代码读取
