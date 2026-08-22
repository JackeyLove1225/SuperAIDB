# SuperAIDB 后端工程规范（AGENTS.md）

## 进程拓扑

- **MCP server（stdio，无端口）**：能力面——全部 37 个数据工具经 MCP 协议暴露给上层 AI
- **Management API :2025**：管理端（表设计器/数据源/文件/dashboard/权限/审批中心）；
  控制台对话由此进程内 open_layer 编排承载（:2024 无独立服务）
- **前端 :3000**：浏览器壳（Next.js，只调 API）

Python 单例均为进程内对象，双进程天然双份——以下规则保证"改配置不重启、状态不串台"。

## 一、配置类状态：ConfigHub 唯一通道（文件即契约）

**一切 YAML/ENV 配置只经 `core/config_hub.py` 读写，禁止进程内另持副本。**

- 读：`load_yaml(path)`（mtime 键缓存，文件没变零读放大，变了原子重读）
- 写：`write_yaml_atomic(path, data, validate=...)`（tmp+replace 原子写 + 自动备份 + 可试载）
- 已接入：PermissionPolicy（permissions.yml）、DataSourceManager（datasources.yml）、
  industries/base（行业目录签名）、settings.INDUSTRY（.env 热键，进程内覆盖优先）、
  MetaDB（行业切换自动换绑）
- **判据**：改配置文件后，任何进程下一次操作必须用到新值——不许重启、不许广播、不许 `reset_instance()` 当饭吃
- 新增配置类状态时：一律经 ConfigHub，不得新开缓存

## 二、会话类状态：归属唯一进程

- **聊天/能力链路会话态**（选择集、force_pending、mutation_pending、pending_unmapped、trace_id）只归 MCP server 进程；mgmt 需要时走 API，禁止"反正各有一份将就用"
- **管理端会话态**只归 Management API 进程
- trace_id 请求级各自生成即可（无需跨进程一致）
- **例外（20260822 起）**：高危人审挂起表（pending_approvals.json）与 sudo 提权状态
  （escalation.json）是**跨进程文件契约**（mtime 新鲜读取）——MCP 进程登记、
  管理端审批中心结算，token 不回传 AI 通道（防 AI 自助结算人审闸）

## 三、资源类：故意进程私有，禁止共享

AIClient（httpx 池）、ChatOpenAI 实例、向量库句柄、驱动/ContractDriver 实例、OCR/embedding 模型、Ladybug 图库连接——
这些是资源不是状态。**各进程各开各的，禁止跨进程传递句柄**（包括以"全局共享"名义注入）。
测试防回归：新代码不得把上述对象放进跨进程可见的容器/文件/队列。

## 四、测试纪律

- 改配置/DDL/DDL 类测试必须走产品工具（drop_column/add_column 会同步 YAML+DB 两层），**禁止绕过产品层直接改 SQL 后不管 YAML**
- 测试夹具的临时配置写 `tests/fixtures/`，不得污染 `config/` 生产配置；污染了必须恢复
- `run_all.py --quick` 是全绿门槛；层 20（配置新鲜度）是 ConfigHub 改动的必过项

## 五、白盒底线（不可让渡）

- AI 禁生成 SQL：所有数据操作走 29 个驱动原子接口 + 决策树确定性路由
- 失败必显式：不静默、不兜底成"假成功"；未映射/冲突/坏行如实报告
- 主键/外键/唯一键/类型由契约层保护，AI 也不可逾越
