# SuperAIDB 架构

## 设计主张

1. **AI 永不生成 SQL**——上层 AI 把用户指令翻译成结构化契约（behavior+object+args，枚举真源=决策树）经 `execute_structured` 确定性路由落到 39 个原子工具（硬路由：MCP 面数据操作仅此一个入口，另有人审三件套与只读辅助共 4 个工具，上层 AI 无法绕过树直接选数据工具、也不能转述自然语言；仓内自然语言链 execute_instruction 保留服务 Web/测试，不上 MCP 面），SQL 由代码在契约层管控下拼装/参数化。每一步可追踪、可调试、可审计（白盒）。条件片段也是封闭的：WHERE 走词法白名单受控拼装，HAVING/ON 走结构化枚举（聚合函数/运算符/标识符引用），值一律参数化或类型字面量——AI 在任何路径上都产不出自由形态 SQL 文本。
2. **能力边界即安全边界**——上层 AI 没有任何裸通道：MCP stdio 是唯一入口，高危操作人审闸（管理端审批中心单点结算，客户端 ask 卡可选叠加），写操作强制契约校验。
3. **接口抽象换实现自由**——29 个驱动接口（27 抽象 + 2 共享）是唯一数据面契约；新增数据库 = 实现接口，上层零改动。daemon 进程隔离就是这个抽象的回报（RPC 代理即插即用）。

## 与主流路线的本质区别（必读）

**vs OpenAI Function Calling（"AI 直接选工具"）**：本架构并不讳言内部仍是 FC 调用（提取意图标签、按工具 schema 提参）——被确定性化的是**工具选择**这一步：弱模型在 39 个工具里直选的犯错面，被压缩到在 7 行为 × 15 对象的封闭枚举里分类（MCP 面上层 AI 直接输出结构化契约 behavior+object，仓内自然语言链另有文本铁证纠偏压过标签错）；选中后每次执行带路由轨迹（`[路由: 查+统计 → aggregate_query]`），可测试、可回归、可审计。主流 FC 把"选哪个工具"永远留在模型黑盒里。

**vs Text2SQL（"AI 生成 SQL"）**：text2sql 的攻击面是任意 SQL 文本——约束解码/语法校验只能管语法，管不了语义正确性与权限语义。本架构的攻击面收窄到封闭参数 schema + 结构化条件片段：表/字段名必须在真实 schema 内（边界闸），条件运算符是封闭枚举，值一律参数化/类型字面量，单语句强制，表列级 RBAC 全程生效。一句话：主流 text2sql 赌"模型生成的 SQL 恰好对"，本架构让"AI 根本没有生成 SQL 的通道"。

**vs LangChain/LangGraph 等 Agent 框架**：编排（任务拆解/多轮/重试）上移给上层 AI 客户端——它是语言无关的 MCP 客户端，随生态免费升级；本仓只保留确定性能力内核。进程内图编排（LangGraph）用过又主动下线：框架的状态机是黑盒，与本项目的白盒纪律不兼容——同一个诉求（可审计），框架给的是日志，本架构给的是确定性路由轨迹 + 挂起表 + 审批中心。

## 分层

```
┌─ 入口面（薄适配，无业务逻辑）──────────────────────────────┐
│  mcp_server.py（MCP stdio，对任意上层 AI）                  │
│  agent/management/（FastAPI 管理 API :2025，Web 控制台后端） │
│  agent-chat-ui（Next.js :3000；开发期独立仓）               │
└──────────────────────┬───────────────────────────────────┘
                       ↓
┌─ 编排资产层（agent/）──────────────────────────────────────┐
│  decision_tree/   确定性路由规则资产（YAML 外置，加载期校验） │
│  router.py        树游走 + 意图标签归一（mutate_natural 消费） │
│  ai_extract.py    自然语言→结构化改/删提取（AI 不碰 SQL）     │
│  （进程内图编排已于 20260824 随对话全量 MCP 化下线：           │
│   上层 AI 客户端即编排器，本仓不再有进程内聊天循环）            │
└──────────────────────┬───────────────────────────────────┘
                       ↓ 唯一通道
┌─ 工具层（agent/tools/，39 个原子工具，按域分包）───────────┐
│  structured / instruct / query / records / ddl / files /    │
│  templates / admin                                         │
│  全部经 core.tool_registry.execute_tool 单点漏斗执行：       │
│  高危人审闸、选择集闸、force 确认闸挂在漏斗上                 │
│  注："原子"=单一职责 + 契约层统一入口 + 参数化执行；           │
│  execute_structured（MCP 面契约入口）与 execute_instruction  │
│  （仓内自然语言链）是编排通道（不属原子工具语义）              │
└──────────────────────┬───────────────────────────────────┘
                       ↓
┌─ 引擎层（core/，不含业务）────────────────────────────────┐
│  data_ops / schema_manager / vector_store（RAG）/           │
│  federation（联邦编排）/ graph（schema 图服务：表关系图谱    │
│  元数据，经 registry 钩子订阅 schema 变更，供表设计器面）    │
└──────────────────────┬───────────────────────────────────┘
┌─ 管线层（pipeline/，顶层包：文件解析→提取→入库编排）────────┐
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
│  联邦写一致性（core/federation/）：ATTACH 挂载写（全 SQLite   │
│  组跨文件原子）/ saga 补偿+journal 续滚（跨引擎）             │
│  栈序铁律：驱动面只向下依赖中立模块（core.sql_safe /         │
│  core.checks / core.check_templates / core.sql_lex /        │
│  core.exceptions / core.crypto / core.logger）——共享校验件   │
│  的唯一实现住中立模块，契约层与驱动层都向下取；向上知识      │
│  （对象归属/表→数据源映射/唯一业务键/表级 schema/联邦 dsm）   │
│  全部经依赖倒置钩子注入（register_* 系列），drivers 与         │
│  permission 白名单由 CI 机器钉死                              │
└──────────────────────┬───────────────────────────────────┘
                       ↓
        物理库（SQLCipher AES-256 静态密文）
```

**层间边的完整口径**（CI 守门 `scripts/ci_dep_direction.py` 的机器验证集）：
- `core ↛ agent`（引擎不感知编排；反向边全部走依赖倒置注册 `core/registry.py`）
- `core ↛ pipeline`（提取管线是上游业务面）
- `core → industries.base` 豁免：industries/ 是**配置层**（行业知识包：schema YAML/术语字典/提示词，无代码逻辑），引擎只允许消费其只读加载器单源
- `drivers ↛ contract/graph`（见上图栈序铁律）
- langgraph 保留面如实声明：进程内图编排已下线，但 `langgraph.types.interrupt` 仍作为图兼容通道的人审原语被 `core/tool_registry.py`/`core/data_ops.py` 引用（`requirements.txt` 钉版保留）——这是有意的兼容取舍，非残留

## 安全栈（自下而上）

- **静态加密**：`core/crypto/`——明文库接入即自动迁移（WAL busy 校验 → 导出 → 验证 → 原子替换）；主密钥默认入 OS 凭据管理器（keyring），系统级隔离模式切 ACL 保护的密钥文件（`db/.vault/master.key`，见下）
- **进程隔离**：`core/daemon/`——数据层收进独立守护进程（常规流量唯一持密钥与句柄的进程），调用方经 DaemonDriver（29 接口 RPC 代理）访问；127.0.0.1 随机端口 + 一次性随机令牌 + 方法白名单 + 会话亲和。**诚实边界**：备份/恢复、认证验密、元数据库、ATTACH 挂载写等少数管理/管线操作按设计在管理端进程内经同一 keyring 通道开库（共用密钥域，非绕过）
- **权限**：`core/permission/`——permissions.yml 热生效；RBAC 角色（admin/user/readonly/自定义用户级角色）+ 表/列级 deny + sudo 提权（escalate_permission → 管理端批准 → TTL 自动降回）
- **人审**：高危工具挂起表 token（一次性、10 分钟、fail-closed），token 不回传 AI 通道，结算只在管理端审批中心（人审收口单点）；**批准必须输入操作密码**（users 表 PBKDF2 慢哈希比对、连续失败锁定——admin token 可能被伪造，密码只在人脑里）；脚本直调路径由契约层直调闸收口（drop_table 等结构高危方法无进程内能力凭证即拒，凭证仅交互式解锁、进程内存、10 分钟 TTL）；上层 AI 客户端侧的审批卡机制（客户端 ask 清单，本仓示例未配）是可叠加的第二层纵深——可选，非必需
- **认证**：管理端 Bearer token（HMAC 签名，角色以库内现值为准，降级即时生效）+ X-API-Key 系统通道；初始管理员密码随机生成、只写用户私有 runtime 目录

## 部署形态

| 形态 | 说明 |
|---|---|
| 默认 | daemon 进程隔离 + 静态加密，开箱即用 |
| 系统级隔离（可选） | `scripts/isolation_setup.ps1`：daemon 切独立服务账号，db//config/运行时目录 ACL 收紧到 {服务账号, 操作者, SYSTEM, Administrators}，其余 OS 账号物理拒绝（含 IPC 令牌） |

## 扩展点

- **新数据库**：实现 `core/drivers/base.py` 的 27 个抽象方法（+2 个共享默认可选覆写），在 `DataSourceManager._DRIVER_FACTORIES` 登记工厂，注册进 `config/datasources.yml`——契约保护自动生效（契约包装在 `get_driver` 单点，新驱动零感知）
- **新行业**：`industries/<name>/` 目录（schema YAML + 术语映射 + prompts），代码零改动
- **新工具**：`agent/tools/` 按域注册 + 决策树 YAML 加叶子；默认不上 MCP 面——要上面须显式加入 `mcp_server._INCLUDE` 白名单并同步 test_32 面断言（工具描述带风险级标注）
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
