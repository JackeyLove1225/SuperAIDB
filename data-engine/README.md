# SuperAIDB

对话式 AI 数据库 Agent：把文件（Excel / PDF / Word / 图片扫描件）变成可查询的结构化数据库，用自然语言完成增删改查、跨库联邦查询与文档检索。行业差异（术语、Prompt、Schema 模板）全部以配置形式放在 `industries/` 下，**换行业不改代码**。

**核心主张：AI 永不生成 SQL。** 意图经受限枚举契约（behavior+object）→ 决策树确定性路由 → 39 个受护栏工具（参数化执行，每步可追踪可审计）——把主流 text2sql 的黑盒变成白盒。设计主张与架构论证见 [ARCHITECTURE.md](ARCHITECTURE.md)。

> 项目源于工程造价行业的真实痛点：PDF 扫描件（定额、清单）人工录入数据库效率极低。先用 n8n 搭建 OCR 解析入库工作流，后为突破本地部署与多数据源联邦查询的瓶颈，以 AI 辅助开发（Vibe Coding）方式迭代为完整产品，内置工程造价行业 Schema 模板与术语映射。

## 产品截图

表结构设计器（工程造价定额库：4 表 / 28 字段 / 3 关系，自动识别主外键并可视化）：

![表设计](docs/demo/e5_designer.png)

> 对话交互不设固定前端——由上层 MCP 客户端（Reasonix / Kimi Code / Claude Code 等）承载，见下方架构说明。

## 架构（2026-08：能力面 MCP 化）

```
途径一：Web 应用（对人）
  agent-chat-ui 前端 :3000（Next.js）
        │
        ▼
  Management API :2025（FastAPI，X-API-Key 认证 + RBAC）
        │
途径二：MCP 能力面（对 AI）
  上层 AI（Reasonix / Kimi Code / Claude Code 等任何 MCP 客户端）
        │  MCP stdio
        ▼
  mcp_server.py —— 全部数据能力经 MCP 协议暴露，高危操作人审闸（审批中心单点结算）
        │
        ▼  两条途径共用同一能力内核
  agent/tools/ —— 39 个原子工具（按域分包：structured/instruct/query/records/ddl/files/templates/admin），MCP 面仅暴露 5 个，其余经 execute_structured 结构化契约路由到达
        │
        ▼
  core/contract —— 安全契约层（标识符白名单 / SQL 注入防护 / 主键约束 / 参数化）
        │
        ▼
  core/drivers —— SQLite（默认）/ MySQL / 联邦驱动（跨库 JOIN 与聚合）
        │
  core/vector_store（Chroma 文档 RAG）· core/parser（Excel/PDF/Word/OCR 解析入库）
        │
        ▼
  industries/ —— 行业配置即代码（Schema YAML + 字段 + Prompt + 术语映射）
```


## 功能特性

- **结构化指令契约（20260905，MCP 面唯一数据通道）**：上层 AI 把用户指令翻译成结构化契约（behavior+object+args，枚举真源=决策树）调用 `execute_structured`——仓内零 LLM 翻译直达树路由，每次执行带路由轨迹可审计；上层 AI 无法绕过树直接选工具，也不能转述自然语言
- **自然语言链（仓内保留）**：Web/测试侧 `execute_instruction`——P1 意图识别（文本铁证零 LLM 优先）→ 决策树确定性路由 → P2 参数提取 → 安全闸执行（不上 MCP 面）
- **39 个原子工具**：表/记录/字段/外键/索引/统计/导入导出等，全部经契约层安全校验；MCP 面仅 5 个数据工具（结构化契约元工具+人审机制+只读辅助）
- **MCP 能力面**：能力内核经 MCP stdio 对任意上层 AI 开放——白名单仅暴露 5 个数据工具（execute_structured 结构化契约+人审机制+只读辅助）；工具描述自带风险级标注（记录级写/结构变更/管理/文件）
- **文件解析入库**：Excel / PDF / Word / 图片（PaddleOCR）解析为结构化数据写入数据库；支持指令级目标聚焦——`tables` 限定目标表、`fields` 限定提取字段子集（如"只把供应商和价格录进 X 表"，runner 层统一收口、链路字段不受影响、零命中如实报）
- **文档 RAG**：上传文档入 Chroma 向量库，回答文档内容类问题
- **多数据源联邦查询**：`config/datasources.yml` 注册多个数据源，支持跨库 JOIN 与聚合
- **跨库写一致性分治**（`core/federation/`）：全 SQLite 写组走 **ATTACH 挂载写**——
  单连接 ATTACH 其余库，savepoint 跨文件原子提交/原子回滚（异常路径真原子；
  WAL 模式下宿主崩溃于 COMMIT 间隙可留部分提交——逐库独立 WAL 无 super-journal，
  相对单库事务是真实放大的毫秒级窗口，如实声明）；含 MySQL 等跨引擎写组走
  **saga 补偿**——逆序补偿 + journal 落盘 + 启动自动续滚（跨引擎物理上无共享事务，
  行业通行取舍）
- **行业配置即代码**：新增行业 = 新增 `industries/` 目录。仓内 `construction_engineering` 仅作示例与回归夹具——产品面向通用能力，各行业用户按同格式自建本行业库
- **表关系可视化**：嵌入式图库（Ladybug，进程内）存储 Schema 图，无需外部图数据库服务
- **Web 管理控制台**：数据源、Schema、行业、权限、系统设置可视化管理

## 安全设计（能力边界即安全边界）

- **契约层强制校验**：所有写操作经标识符白名单、SQL 注入防护、值参数化；系统表（用户/权限资产）数据面写整表永久拒绝（SQL 直改变体同拦）
- **MCP 通道 fail-closed**：`MCP_USER` 绑定具体用户后与控制台同走用户权限通道（用户级规则/自助收紧全生效）；未配置或用户不存在直接拒绝启动（连只读都不给——只读亦是信息泄露面）
- **sudo 提权 + 人审闸**：AI 需管理员权限时经 `escalate_permission` 申请，人审批准后临时提权；高危工具挂起表 token 一次性、10 分钟有期、fail-closed；token 不回传 AI 通道，结算只在管理端审批中心（防 AI 自助人审）
- **人审收口单点 + 同步回执**：高危操作一律挂起，仅在管理端审批中心结算（token 不出 AI 通道）；MCP 通道同步等待审批结果，批准/拒绝/异常如实回传 AI 对话；上层 AI 客户端的 ask 审批卡是可叠加的第二层纵深（可选，非必需）
- **权限体系**：数据源/表/列三级级联（上级禁止不可被下级解禁）+ 按用户授权（users 段）+ 自助收紧（deny-only）；管理端点仅限 admin；高危写操作另需操作密码
- **全量安全测试**：SQL 注入/危险语句拦截、权限矩阵、认证全链路、加密边界、daemon 隔离、人审双通道（MCP/管理端两进程链）均有专项回归层（test_14/17/21/34/35/32 等），CI 每个 push 真跑

## 部署安全边界（数据主权边界 = 加密边界）

**边界内（接入本产品的库）我负责**：静态密文（SQLCipher AES-256，密钥由 OS 凭据管理器保管）+ 运行时数据面操作收进独立守护进程（daemon，29 接口 RPC 代理，上层零感知）+ 全部操作过契约/权限/人审。

- **接入即加密**：明文 SQLite 库在注册/打开瞬间自动迁移为密文（迁移验证通过后默认删除明文备份，`MIGRATE_KEEP_PLAIN_BACKUP=1` 可保留排障）；`DB_ENCRYPT=false` 可显式回退（不推荐）
- **运行时隔离（默认开）**：`DAEMON_MODE=true` 时驱动调用经 `core/daemon/` 进程；密钥驻留 daemon 内存，不落盘。**诚实边界**：少数管理/管线操作按设计在管理端进程内经同一 keyring 通道开库——备份/恢复、认证验密、元数据库、以及 ATTACH 挂载写（跨 SQLite 组真原子写入）；这些路径与 daemon 共用同一密钥域而非绕过它，常规 AI/查询/工具写流量仍全部经 daemon RPC
- **物理隔离（企业选项）**：`scripts/isolation_setup.ps1` 一键三态（enable/disable/status）——daemon 切专用服务账号 + db//config/运行时目录 ACL 收紧（含密钥交接），其他 OS 账号物理读不到数据文件与 IPC 令牌，与加密构成双重保险

**边界外（未接入的文件/服务型库）**：操作系统层面的访问不是本产品的防护范围（任何软件都管不了边界外的文件）。服务型库（MySQL/SQL Server）的连接凭据目前在 `config/datasources.yml` 配置——该文件已入 AI 文件工具黑名单（AI 不可读），并随系统级隔离脚本做 ACL 收紧；凭据入 OS 凭据管理器是既定演进方向。

**OCR 出网提示**：图片/扫描件文字识别默认走 PaddleOCR 云服务（`OCR_API_TOKEN` 配置后生效）——扫描件内容会发送到云端识别，此链路在加密边界之外；数据敏感场景请用 `OCR_API_URL` 指向自建/可信端点，或不配 token（扫描件如实降级、不识别）。

**诚实声明的残差**：同一 OS 账号下的管理员级蓄意攻击（注入/调试 daemon 进程取内存密钥）不在防护范围——这是所有同类软件的共同底线，三期跨账号部署才覆盖。本地无密码模式（`API_KEY_ENABLED=false`）的信任边界 = 本机回环令牌（`config/runtime/loopback.token`，启动期轮换 + 0600 权限）：一切写方法（审批/权限/备份/停机/数据 CRUD/DDL/上传/学习）经中间件强制令牌，读方法对本机开放；同机其他 OS 账号默认读不到令牌（跨账号物理隔离见 `scripts/isolation_setup.ps1`）。前端代理默认只绑 `127.0.0.1`（package.json scripts 钉死）——局域网不可达，回环节面不暴露到网络；需要对外提供服务的部署必须改开 `API_KEY_ENABLED=true` 认证模式，不要靠改绑定地址暴露无密码模式。
另有补偿快照明文残差如实说：联邦 saga 的 journal（`db/saga_journal/saga_*.json`）为崩溃续滚存整行明文快照，在整库加密边界之外（该目录已入文件收容闸黑名单，不得经文件工具入库扩散）。

## 技术栈

- **语言/平台**：Python 3.13；目标平台 Windows 10/11 桌面端（.bat 启动脚本 + `vendor/` 内 Windows 预编译 Ladybug 组件，均为有意决策）
- **AI 接入**：OpenAI 兼容 API（默认 DeepSeek），供文件提取/改删结构提取等机械 AI 调用
- **MCP**：mcp python-sdk ≥ 1.29（`mcp_server.py`，stdio 传输）
- **管理 API**：FastAPI + Uvicorn
- **数据库**：SQLite（默认）/ MySQL；嵌入式图库 Ladybug（Schema 图）
- **向量库**：Chroma
- **文件解析**：openpyxl / PyMuPDF / python-docx / PaddleOCR，LibreOffice（可选，Office 预览）
- **前端**：[`agent-chat-ui`](../agent-chat-ui)（Next.js，pnpm；开发期独立仓，开源发布与本仓合并快照）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp config/.env.example config/.env
```

`config/.env.example` 内有分组注释（33+ 个键），最少需要填：

| 键 | 说明 |
|---|---|
| `AI_API_KEY` | AI 接口密钥（必填，默认 DeepSeek） |
| `AI_BASE_URL` / `AI_MODEL` | 接口地址 / 模型名（默认 `https://api.deepseek.com` / `deepseek-chat`） |
| `DB_TYPE` | `sqlite`（默认）或 `mysql` |
| `INDUSTRY` | 行业模式：`construction_engineering`（示例 Schema 包，各行业可自建） |
| `API_KEY_ENABLED` | 部署到服务器时置 `true` 开启 Management API 强制认证（Bearer 用户认证；X-API-Key 系统通道已废除，见 docs/X-API-Key系统通道废除说明.md）；本地 `false` 为开发模式（信任边界=本机回环令牌） |

多数据源（联邦查询）在 `config/datasources.yml` 注册。

### 3. 启动 Web 应用

**一键启动（推荐）**：

```bat
start_web.bat
```

launcher 并行拉起 Management API（:2025）→ 前端（:3000），自动打开浏览器，带系统托盘图标。`start.bat` 为控制台调试模式；`stop.bat` 停止全部服务。

手动启动：

```bash
# 终端 1：Management API
python -m uvicorn agent.management.server:mgmt_app --port 2025 --host 127.0.0.1

# 终端 2：前端（在 ../agent-chat-ui 目录）
cd ../agent-chat-ui && pnpm dev
```

启动后：管理与可视化控制台 http://localhost:3000 ，API 文档 http://localhost:2025/docs 。
（对话交互不设固定前端——由任意 MCP 客户端承载，见「4. 接入上层 AI」）

### 4. 接入上层 AI（MCP 方式）

在任何 MCP 客户端中注册本引擎，以 Reasonix 配置为例：

```toml
[[plugins]]
name = "data-engine"
command = "python"
args = ["<本仓库绝对路径>/data-engine/mcp_server.py"]   # 替换为本机实际路径
env = { MCP_USER = "admin" }   # MCP 通道绑定的用户名（users 表中的用户；缺省=只读）
```

`MCP_USER` 在 MCP 客户端的 env 块配置（不走 `.env`——mcp_server 只读进程
环境变量）：绑定后该客户端的 AI 以该用户身份操作（用户级规则/自助收紧
全生效）；缺省或绑定不存在的用户一律只读（fail-closed）。

Claude Code 一行命令：`claude mcp add data-engine -- python "<本仓库绝对路径>/data-engine/mcp_server.py"`

演示素材：`sample_data/演示定额样例.pdf`（自造合成定额表，无版权问题，可直接用于文件入库演示）。

上层 AI 只能看到 5 个数据工具：`execute_structured`（结构化契约通道，唯一数据入口）+ 人审机制三件套 + 只读辅助；写/DDL/查询等 34 个工具已撤出 MCP 面（33 个具体工具+自然语言链），只能经结构化契约的树路由到达（按名直调在调用点封死）。高危写操作在通道内触发人审闸，到管理端审批中心批准后才执行。

## 测试

```bash
pip install -r requirements-dev.txt   # 测试/门禁工具链（pytest/coverage/ruff，钉版本）
python tests/run_all.py --quick       # 26 个离线测试层（CI 用，无需外部服务/AI Key）
python tests/run_all.py --list        # 列出所有测试层
```

离线层覆盖：编译检查、工具注册与路由、Excel 解析、联邦数据库、Schema 一致性、SQL 注入安全、权限与认证全链路、MCP 能力面与人审跨进程链等。
另有手动演示验收资产 `tests/acceptance/`（S1–S11 端到端脚本，Node 运行，需前端+后端已启动；CI 不自动跑）。

## 项目结构

```
agent/          # 编排层：decision_tree（确定性路由规则资产）、router、ai_extract（改删结构提取）、
                #   tools/（39 工具注册，按域分包：structured/instruct/query/records/ddl/files/templates/admin）、
                #   management/（FastAPI 管理 API + launcher）
core/           # 引擎层（不含业务）：drivers/（29 个原子接口，含事务）、contract/（安全契约）、
                #   parser/（文件解析）、federation/（联邦查询）、vector_store/（RAG）、
                #   permission/（RBAC + sudo 提权）
pipeline/       # 管线层：文件解析 → 统一中间格式提取 → 入库编排（挂载写/saga 分治）
industries/     # 行业配置即代码（construction_engineering 为示例与回归夹具，行业库由用户自建）
mcp_server.py   # MCP 能力面入口（stdio，结构化契约：面仅 5 个数据工具，其余经 execute_structured 树路由到达）
reasonix.toml   # Reasonix 客户端集成示例：权限边界 deny 规则 + MCP 插件注册
tests/          # 26 层回归测试（--quick 跑 26 个离线层，CI 门禁）
docs/           # 文档（testing/ 测试清单与报告、demo/ 产品截图）
scripts/        # 部署与运维脚本（debug/ 为开发期调试探针）
uploads/        # 用户上传文件暂存（首次上传时自动创建，无需手工建目录）
```

## 许可与权属

本项目著作权归 **陈龙（JackeyLove1225）** 所有，采用**双轨授权（Dual Licensing）**（见 [LICENSE](LICENSE)）：

- 📖 **AGPL v3（社区轨）**：允许个人学习、评估与开源社区使用；基于本项目构建并对外提供服务（含 SaaS）的衍生作品，须以 AGPL v3 公开完整源代码
- 💼 **商业授权（企业轨）**：如需闭源商用、生产部署或集成进商业产品，请联系作者购买商业授权 → Jackeylove1225@163.com

本项目采用双轨授权；公开开源版本见 github.com/JackeyLove1225/SuperAIDB。
