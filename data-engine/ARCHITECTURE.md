# SuperAIDB 架构

## 设计主张

1. **AI 永不生成 SQL**——自然语言意图经确定性路由落到 38 个原子工具，SQL 由代码在契约层管控下拼装/参数化。AI 只在受限枚举空间做判断，每一步可追踪、可调试、可审计（白盒）。
2. **能力边界即安全边界**——上层 AI 没有任何裸通道：MCP stdio 是唯一入口，高危操作双层人审，写操作强制契约校验。
3. **接口抽象换实现自由**——29 个驱动接口（27 抽象 + 2 共享）是唯一数据面契约；新增数据库 = 实现接口，上层零改动。daemon 进程隔离就是这个抽象的回报（RPC 代理即插即用）。

## 分层

```
┌─ 入口面（薄适配，无业务逻辑）──────────────────────────────┐
│  mcp_server.py（MCP stdio，对任意上层 AI）                  │
│  agent/management/（FastAPI 管理 API :2025，Web 控制台后端） │
│  agent-chat-ui（Next.js :3000；开发期独立仓）               │
└──────────────────────┬───────────────────────────────────┘
                       ↓
┌─ 编排层（agent/）─────────────────────────────────────────┐
│  open_layer/      意图理解 → 任务拆解 → 观察-调整循环 → 综合  │
│  decision_tree/   确定性路由（YAML 外置，加载期 4 项结构校验）│
│  router.py        树游走 + LLM 意图标签的确定性纠偏          │
└──────────────────────┬───────────────────────────────────┘
                       ↓ 唯一通道
┌─ 工具层（agent/tools/，38 个原子工具，按域分包）───────────┐
│  query / records / ddl / files / templates / admin         │
│  全部经 core.tool_registry.execute_tool 单点漏斗执行：       │
│  高危人审闸、选择集闸、force 确认闸挂在漏斗上                 │
└──────────────────────┬───────────────────────────────────┘
                       ↓
┌─ 引擎层（core/，不含业务）────────────────────────────────┐
│  data_ops / schema_manager / pipeline（文件解析入库）/       │
│  vector_store（RAG）/ federation（联邦编排）                │
└──────────────────────┬───────────────────────────────────┘
                       ↓ 唯一货源
┌─ DataSourceManager：懒加载驱动，出厂即包 ContractDriver ────┐
└──────────────────────┬───────────────────────────────────┘
                       ↓
┌─ 契约栈（core/contract/）─────────────────────────────────┐
│  标识符白名单 / WHERE 与 SET 校验 / 值参数化 / 单语句强制    │
│  权限单栈：表级 RBAC + 列级屏蔽（内置凭证列恒 deny）          │
│  裸 SQL 护栏（execute 透传口）+ 错误翻译                     │
└──────────────────────┬───────────────────────────────────┘
                       ↓
┌─ 驱动面（core/drivers/）Driver ABC 29 接口                 │
│  sqlite / mysql / federated（表级路由）/ daemon RPC 代理     │
└──────────────────────┬───────────────────────────────────┘
                       ↓
        物理库（SQLCipher AES-256 静态密文）
```

## 安全栈（自下而上）

- **静态加密**：`core/crypto/`——明文库接入即自动迁移（WAL busy 校验 → 导出 → 验证 → 原子替换）；主密钥默认入 OS 凭据管理器（keyring），系统级隔离模式切 ACL 保护的密钥文件（`db/.vault/master.key`，见下）
- **进程隔离**：`core/daemon/`——数据层收进独立守护进程（唯一持密钥与句柄），调用方经 DaemonDriver（29 接口 RPC 代理）访问；127.0.0.1 随机端口 + 一次性随机令牌 + 方法白名单 + 会话亲和
- **权限**：`core/permission/`——permissions.yml 热生效；RBAC 角色（admin/user/readonly/自定义用户级角色）+ 表/列级 deny + sudo 提权（escalate_permission → 管理端批准 → TTL 自动降回）
- **人审**：高危工具挂起表 token（一次性、10 分钟、fail-closed），token 不回传 AI 通道，结算只在管理端审批中心；上层 AI 客户端侧审批卡（reasonix.toml 的 ask 清单）为第二层纵深
- **认证**：管理端 Bearer token（HMAC 签名，角色以库内现值为准，降级即时生效）+ X-API-Key 系统通道；初始管理员密码随机生成、只写用户私有 runtime 目录

## 部署形态

| 形态 | 说明 |
|---|---|
| 默认 | daemon 进程隔离 + 静态加密，开箱即用 |
| 系统级隔离（可选） | `scripts/isolation_setup.ps1`：daemon 切独立服务账号，db//config/运行时目录 ACL 收紧到 {服务账号, 操作者, SYSTEM, Administrators}，其余 OS 账号物理拒绝（含 IPC 令牌） |

## 扩展点

- **新数据库**：实现 `core/drivers/base.py` 的 27 个抽象方法，注册进 `config/datasources.yml`——契约保护自动生效
- **新行业**：`industries/<name>/` 目录（schema YAML + 术语映射 + prompts），代码零改动
- **新工具**：`agent/tools/` 按域注册 + 决策树 YAML 加叶子；MCP 能力面自动暴露（工具描述带风险级标注）
- **新上层 AI**：任何 MCP 客户端直连 `mcp_server.py`

## 附录：大规模表结构管理方案（>500 表演进方向）

### 当前（YAML-only，适用于 <500 张表）

```
industries/<行业>/schemas/
├── province.yaml
├── quota_base.yaml
└── ...（每张表一个 YAML 文件）
```

运行时 `_load_schemas()` 遍历目录读文件。

### 未来（YAML + DB Registry，适用于 500-10000 张表）

```
┌─ 开发层（YAML 源码，Git 管理）──────┐
│  schemas/（只放行业标准表）          │
│  每次编辑 YAML 后提交 Git            │
└─────────────────────────────────────┘
          ↓ 启动时自动同步
┌─ 运行层（DB _schema_registry 表）────┐
│  table_name | columns_json | source  │
│  自定义表直接写 registry，不生成文件  │
└─────────────────────────────────────┘
          ↓ 一次加载
┌─ 缓存层（Python 字典）──────────────┐
│  进程内缓存，零文件 I/O             │
└─────────────────────────────────────┘
```

| 原则 | 怎么保证 |
|:-----|:---------|
| **YAML-first** | 标准表必须从 YAML 来；registry 中 `source=yaml` 的表只读 |
| **配置驱动** | 用户自定义表走 `batch_create_tables` 流程（先写配置、后建 DB） |
| **运行时无文件 I/O** | 启动时从 registry 表加载一次到内存 |
