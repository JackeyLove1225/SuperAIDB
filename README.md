# SuperAIDB

[![License: AGPL v3 / 商业双轨](https://img.shields.io/badge/License-AGPL%20v3%20%2B%20Commercial-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio%20能力面-green.svg)](data-engine/mcp_server.py)
[![CI](https://github.com/JackeyLove1225/SuperAIDB/actions/workflows/ci.yml/badge.svg)](https://github.com/JackeyLove1225/SuperAIDB/actions/workflows/ci.yml)

对话式 AI 数据库 Agent：把文件（Excel / PDF / Word / 图片扫描件）变成可查询的结构化数据库，用自然语言完成增删改查、跨库联邦查询与文档检索。行业差异（术语、Prompt、Schema 模板）全部以配置形式放在 `industries/` 下，**换行业不改代码**。

**核心主张：AI 永不生成 SQL。** 上层 AI 把用户指令翻译成结构化契约（behavior+object+args，枚举真源=决策树）经 `execute_structured` 确定性树路由落到 39 个原子工具——上层 AI 无法绕过树直接选工具，也不转述自然语言，每步可追踪可审计，把主流 text2sql 的黑盒变成白盒——设计主张详见 [data-engine/ARCHITECTURE.md](data-engine/ARCHITECTURE.md)。

> 项目源于工程造价行业的真实痛点：PDF 扫描件（定额、清单）人工录入数据库效率极低——为此构建了完整的 OCR 解析入库与自然语言查询产品。仓内工程造价行业包仅作示例与回归夹具；产品面向通用能力，任何行业都可按同格式自建本行业库。

## 产品截图

表结构设计器（工程造价定额库：4 表 / 28 字段 / 3 关系，自动识别主外键并可视化）：

![表设计器](assets/designer.png)

细粒度权限管理（库/表/列/角色四级控权，保存即生效，规则可干跑预演）：

![权限管理](assets/permissions.png)

> 对话交互不设固定前端——由上层 MCP 客户端（Reasonix / Kimi Code / Claude Code 等）承载，见下方架构说明。

## 仓库布局

本仓库为单仓库（monorepo）快照，两个子项目：

```
data-engine/      # 产品核心：MCP 能力面（结构化指令契约）+ 39 原子工具 + 契约安全层 + 管理 API（详见其 README）
agent-chat-ui/    # Web 管理控制台（Next.js）：Schema 设计器/数据源/权限/行业管理
```

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
  mcp_server.py —— 全部数据能力经 MCP 协议暴露，高危操作人审闸（审批中心单点结算，AI 客户端同步等待审批结果）
        │
        ▼  两条途径共用同一能力内核
  agent/tools/ —— 39 个原子工具（按域分包），MCP 面仅暴露 5 个，其余经 execute_structured 结构化契约树路由到达
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

- **结构化指令契约（MCP 面唯一数据通道）**：上层 AI 把用户指令翻译成结构化契约（behavior+object+args，枚举真源=决策树）调用 `execute_structured`——仓内零 LLM 翻译直达树路由，每次执行带路由轨迹可审计；上层 AI 无法绕过树直接选工具，也不能转述自然语言
- **39 个原子工具**：表/记录/字段/外键/索引/统计/导入导出等，全部经契约层安全校验；MCP 面仅 5 个数据工具（结构化契约元工具+人审机制+只读辅助）
- **MCP 能力面**：能力内核经 MCP stdio 对任意上层 AI 开放——白名单仅暴露 5 个数据工具，其余经 `execute_structured` 树路由到达；工具描述自带风险级标注（记录级写/结构变更/管理/文件）
- **文件解析入库**：Excel / PDF / Word / 图片（PaddleOCR）解析为结构化数据写入数据库；支持指令级目标聚焦（`tables` 限定目标表、`fields` 限定提取字段子集——"只把供应商和价格录进 X 表"类指令，runner 层统一收口、零命中如实报）
- **文档 RAG**：上传文档入 Chroma 向量库，回答文档内容类问题
- **多数据源联邦查询**：`config/datasources.yml` 注册多个数据源，支持跨库 JOIN 与聚合
- **行业配置即代码**：新增行业 = 新增 `industries/` 目录。仓内 `construction_engineering` 仅作示例与回归夹具——产品面向通用能力，各行业用户按同格式自建本行业库
- **表关系可视化**：嵌入式图库（Ladybug，进程内）存储 Schema 图，无需外部图数据库服务
- **Web 管理控制台**：数据源、Schema、行业、权限、系统设置可视化管理

## 安全设计（能力边界即安全边界）

- **契约层强制校验**：所有写操作经标识符白名单、SQL 注入防护、值参数化；系统表（用户/权限资产）数据面写整表永久拒绝（SQL 直改变体同拦）
- **MCP 通道 fail-closed**：`MCP_USER` 绑定具体用户后与控制台同走用户权限通道（用户级规则/自助收紧全生效）；未配置或用户不存在直接拒绝启动（连只读都不给）
- **sudo 提权 + 人审闸**：AI 需管理员权限时经 `escalate_permission` 申请，人审批准后临时提权；高危工具挂起表 token 一次性、10 分钟有期、fail-closed；token 不回传 AI 通道，结算只在管理端审批中心（防 AI 自助人审）
- **人审收口单点 + 同步回执**：高危操作一律挂起，仅在管理端审批中心结算（token 不出 AI 通道）；MCP 通道同步等待审批结果，批准/拒绝/异常如实回传 AI 对话；上层 AI 客户端的 ask 审批卡是可叠加的第二层纵深（可选，非必需）
- **RBAC 权限体系**：用户级专属角色，支持表/列级细粒度权限；管理端点仅限 admin
- **全量安全测试**：SQL 注入/权限矩阵/认证/加密边界/daemon 隔离/人审双通道均有专项回归层，CI 每个 push 真跑

## 技术栈

- **语言**：Python 3.13（Windows；启动脚本为 .bat）
- **AI 接入**：OpenAI 兼容 API（默认 DeepSeek），供文件提取/改删结构提取等机械 AI 调用
- **MCP**：mcp python-sdk ≥ 1.29（`mcp_server.py`，stdio 传输）
- **管理 API**：FastAPI + Uvicorn
- **数据库**：SQLite（默认）/ MySQL；嵌入式图库 Ladybug（Schema 图）
- **向量库**：Chroma
- **文件解析**：openpyxl / PyMuPDF / python-docx / PaddleOCR，LibreOffice（可选，Office 预览）
- **前端**：同仓库 [`agent-chat-ui/`](agent-chat-ui)（Next.js，pnpm）

## 快速开始

### 1. 安装依赖

```bash
pip install -r data-engine/requirements.txt
```

### 2. 配置环境变量

```bash
cp data-engine/config/.env.example data-engine/config/.env
```

`data-engine/config/.env.example` 内有分组注释（33+ 个键），最少需要填：

| 键 | 说明 |
|---|---|
| `AI_API_KEY` | AI 接口密钥（必填，默认 DeepSeek） |
| `AI_BASE_URL` / `AI_MODEL` | 接口地址 / 模型名（默认 `https://api.deepseek.com` / `deepseek-chat`） |
| `DB_TYPE` | `sqlite`（默认）或 `mysql` |
| `INDUSTRY` | 行业模式：`construction_engineering`（内置定额库 Schema 包） |
| `API_KEY_ENABLED` / `API_KEY` | 部署到服务器时开启 Management API 的 X-API-Key 认证 |

多数据源（联邦查询）在 `data-engine/config/datasources.yml` 注册。

### 3. 启动 Web 应用

**一键启动（推荐）**：

```bat
cd data-engine && start_web.bat
```

launcher 并行拉起 Management API（:2025）→ 前端（:3000），自动打开浏览器，带系统托盘图标。`start.bat` 为控制台调试模式；`stop.bat` 停止全部服务。

手动启动：

```bash
# 终端 1：Management API
cd data-engine && python -m uvicorn agent.management.server:mgmt_app --port 2025 --host 127.0.0.1

# 终端 2：前端
cd agent-chat-ui && pnpm dev
```

启动后：管理与可视化控制台 http://localhost:3000 ，API 文档 http://localhost:2025/docs 。

### 4. 接入上层 AI（MCP 方式）

在任何 MCP 客户端中注册本引擎，以 Reasonix 配置为例：

```toml
[[plugins]]
name = "data-engine"
command = "python"
args = ["<本仓库绝对路径>/data-engine/mcp_server.py"]
env = { MCP_USER = "admin" }   # MCP 通道绑定的用户名（必填：users 表中存在的用户）
```

`MCP_USER` 在 MCP 客户端的 env 块配置（不走 `.env`）：绑定后该通道与控制台
同走此用户的权限通道（角色/用户级/自助规则全生效）；未配置或用户不存在
则拒绝启动（fail-closed——未绑定通道连只读都不给，只读亦是信息泄露面）。

上层 AI 只能看到 5 个数据工具：`execute_structured`（结构化契约通道，唯一数据入口）+ 人审机制三件套 + 只读辅助；写/DDL/查询等 34 个工具已撤出 MCP 面（33 个具体工具+自然语言链），只能经结构化契约的树路由到达（按名直调在调用点封死）。高危写操作在通道内触发人审闸：AI 对话同步等待，人工在管理端审批中心（admin + 操作密码）批准后结果如实回传，拒绝同理。

## 测试

```bash
cd data-engine
python tests/run_all.py --quick    # 26 个离线测试层（CI 用，无需外部服务/AI Key）
python tests/run_all.py --list     # 列出所有测试层
```

离线层覆盖：编译检查、工具注册与路由、Excel 解析、联邦数据库、Schema 一致性、SQL 注入安全、权限与认证全链路、MCP 能力面与人审双通道等。

## 项目结构

`data-engine/`（产品核心）内部结构：

```
agent/          # 编排层：decision_tree（确定性路由规则资产）、router、ai_extract（改删结构提取）、
                #   tools/（39 工具注册，按域分包）、management/（FastAPI 管理 API + launcher）
core/           # 引擎层（不含业务）：drivers/（29 个原子接口，含事务）、contract/（安全契约）、
                #   parser/（文件解析）、federation/（联邦查询）、vector_store/（RAG）、
                #   permission/（RBAC + sudo 提权）
industries/     # 行业配置即代码（construction_engineering 为示例与回归夹具，行业库由用户自建）
mcp_server.py   # MCP 能力面入口（stdio，结构化契约：面仅 5 个数据工具，其余经 execute_structured 树路由到达）
reasonix.toml   # Reasonix 客户端集成示例：权限边界 deny 规则 + MCP 插件注册
tests/          # 26 层回归测试（--quick 跑 26 个离线层，CI 门禁；数字口径由层 39 机器守护，见 data-engine/README.md）
docs/           # 文档（testing/ 测试清单与报告、demo/ 产品截图）
scripts/        # 部署与运维脚本（debug/ 为开发期调试探针）
```

## 许可与权属

本项目著作权归 **陈龙（JackeyLove1225）** 所有，采用**双轨授权（Dual Licensing）**（见 [LICENSE](LICENSE)）：

- 📖 **AGPL v3（社区轨）**：允许个人学习、评估与开源社区使用；基于本项目构建并对外提供服务（含 SaaS）的衍生作品，须以 AGPL v3 公开完整源代码
- 💼 **商业授权（企业轨）**：如需闭源商用、生产部署或集成进商业产品，请联系作者购买商业授权 → Jackeylove1225@163.com

本仓库为开源发布版本；日常迭代在私有开发仓库持续进行。
