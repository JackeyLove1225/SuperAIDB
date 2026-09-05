# SuperAIDB 管理控制台（agent-chat-ui）

> 基于 [langchain-ai/agent-chat-ui](https://github.com/langchain-ai/agent-chat-ui)（MIT）二次开发。

SuperAIDB 的 Web 管理控制台：面向「人」的可视化入口——Schema 设计器、数据源管理、表数据浏览与编辑、行业管理、系统设置。

对话由任意 MCP 客户端（Claude Code / Kimi Code 等）直连后端的 `mcp_server.py` 完成；本控制台专注管理可视化。

配套后端：[`JackeyLove1225/SuperAIDB`](https://github.com/JackeyLove1225/SuperAIDB)（AGPL v3 + 商业授权双轨制）。

## 功能

- **Schema 设计器**（`/dashboard/schema-designer`）：可视化设计表结构、字段、外键关系（React Flow 拖拽卡片 + 外键连线）
- **管理控制台**（`/dashboard`）：数据库总览、后端状态、运行指标
- **数据源管理**（`/dashboard/datasources`）：查看/管理联邦数据源（对应后端 `config/datasources.yml`）
- **表数据编辑**（`/dashboard/tables/[tableName]`）：浏览和编辑单表记录
- **行业管理**（`/settings`）：切换行业、编辑行业配置、AI 向导（行业库按「配置即代码」自建）
- **系统设置**（`/settings`）：开发者模式开关、系统信息、后端服务控制

## 快速开始

### 环境要求

- Node.js（建议 20+）与 pnpm（本仓库锁定 `pnpm@10.5.1`）
- 后端 SuperAIDB 已启动（Management API :2025），参见后端仓库 README

### 1. 安装依赖

```bash
pnpm install
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

| 变量           | 默认/必填               | 说明                                                    |
| -------------- | ----------------------- | ------------------------------------------------------- |
| `MGMT_API_URL` | `http://localhost:2025` | Management API 地址（服务端代理用，浏览器不直连）       |
| `MGMT_API_KEY` | **必填**                | 须与后端 `API_KEY` 一致；仅存在于服务端，不下发到浏览器 |

### 3. 启动

```bash
pnpm dev   # http://localhost:3000
```

根路径自动重定向到 Schema 设计器。

## 许可

- 二次开发部分著作权归 **陈龙（JackeyLove1225）** 所有
- 上游代码遵循其 MIT 许可（见 [LICENSE](LICENSE)，保留上游版权声明）
