"""graph 子包·全自动建库流程（build_db）：探索 → 设计 → 人工确认 → 建表

由原 graph.py 拆分而来（facade 模式，纯搬家不改逻辑）。

设计意图：用户上传文件夹并说"建成数据库"后，AI 先读文件样本，
自主设计表结构和表关系，经【人工确认】后建表并逐文件入库
（表格→SQLite，纯文本→向量库）。除确认环节外全程无人工干预。
"""

from core.logger import get_logger

logger = get_logger(__name__)

from langgraph.runtime import Runtime

from config.settings import settings
from agent.open_layer.state import AgentState, FileContext
from agent.open_layer.prompts import extract_text_from_content, format_file_manifest

# 经 facade 调用时取值：_get_llm 被测试 patch.object(graph, "_get_llm") 遮蔽，
# 节点内必须读 facade 上的当前值（循环导入安全：本模块只经 facade 导入，
# 被导入时 facade 已在 sys.modules 中，属性到调用时才解析）
from agent.open_layer import graph as _g

_BUILD_SUPPORTED_PIPELINE_EXTS = (".pdf", ".xlsx", ".xls", ".docx")
_BUILD_TEXT_EXTS = (".txt", ".md", ".csv", ".json")

# 建库事实汇总标记——build_db_create 末尾生成的确定性汇报以此开头，
# synthesize_result 据此直接采用该汇报（不调 LLM 综合，防止"声称导入3条实际0条"的不实汇报）
_BUILD_DB_SUMMARY_MARKER = "📊 建库执行汇报"


def _is_create_failure(res) -> bool:
    """建表结果是否失败（结构化优先：ToolResult.data.ok=False 即失败；
    legacy 文本路径保留旧判定——操作失败/参数校验失败/校验拦截/同名冲突 都算）"""
    if not res:
        return False
    from core.tool_result import ToolResult
    if isinstance(res, ToolResult):
        ok = res.data.get("ok")
        if ok is not None:
            return not ok
        res = res.text  # legacy：落到文本判定
    return (res.startswith(("操作失败", "参数校验失败", "校验拦截"))
            or "（生成后校验拦截）" in res
            or "需要覆盖吗" in res)


def _build_db_fact_summary(create_res: str | None, definitions: list,
                           ingest_facts: list, abort_reason: str = "") -> str:
    """生成建库流程的确定性事实汇总（不经过 LLM，数字全部来自实际执行结果）

    Args:
        create_res: 建表执行结果文本（None 表示未执行到建表）
        definitions: 本次建表的 definitions 列表
        ingest_facts: 逐文件入库事实 [(path, status, detail)]，
                      status ∈ 成功/失败/跳过
        abort_reason: 流程中止原因（如未获得有效表结构设计）
    """
    lines = [_BUILD_DB_SUMMARY_MARKER + "（基于实际执行结果）"]
    if abort_reason:
        lines.append(f"建库中止：{abort_reason}")
        return "\n".join(lines)

    # 建表事实
    if create_res is None:
        lines.append("建表：未执行")
    elif _is_create_failure(create_res):
        lines.append(f"建表：失败（{create_res[:120]}）")
    else:
        names = [d.get("name", "?") for d in definitions]
        lines.append(f"建表：成功 {len(names)} 张（{', '.join(names)}）")

    # 逐文件入库事实
    if ingest_facts:
        lines.append("文件入库明细：")
        ok_files = fail_files = skip_files = 0
        for path, status, detail in ingest_facts:
            lines.append(f"- {path}：{status}（{detail}）" if detail else f"- {path}：{status}")
            if status == "成功":
                ok_files += 1
            elif status == "失败":
                fail_files += 1
            else:
                skip_files += 1
        lines.append(f"合计：成功 {ok_files} 个文件，失败 {fail_files} 个，跳过 {skip_files} 个")
    elif create_res is not None and not _is_create_failure(create_res):
        lines.append("文件入库明细：无文件需要入库")

    return "\n".join(lines)


def _get_file_ctx(runtime) -> tuple[list, dict]:
    """从 runtime.context 提取文件清单和内容（无则返回空）

    建库集合优先：前端会把 origin=build 的文件单独放进 build_manifest——
    浏览区的文件（用户可能只是看看）绝不被建库流程处理。
    """
    ctx = runtime.context if (runtime and runtime.context) else {}
    if not isinstance(ctx, dict):
        return [], {}
    manifest = ctx.get("build_manifest") or ctx.get("file_manifest") or []
    return manifest, ctx.get("file_contents", {}) or {}


def build_db_explore(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """建库流程·探索：读取代表性文件样本，供 schema 设计使用

    表格/文档类优先（它们是建表依据），最多 3 个样本：
    - 文本类：直接用 file_contents 中的内容
    - 二进制类（xlsx/pdf/docx）：经 file_tools.read_file 解析服务器落盘文件
    """
    from pathlib import Path as _P

    manifest, contents = _get_file_ctx(runtime)
    PRIORITY = ("spreadsheet", "document", "pdf")
    ranked = sorted(
        manifest,
        key=lambda e: PRIORITY.index(e["category"]) if e.get("category") in PRIORITY else 99,
    )
    samples = []
    for entry in ranked[:3]:
        path = entry.get("path", "")
        server_path = entry.get("server_path", "")
        text = contents.get(path, "")
        if not text or text.startswith("["):
            if server_path and _P(server_path).exists():
                try:
                    from agent.open_layer.file_tools import read_file
                    text = str(read_file(server_path))
                except Exception as e:
                    text = f"(读取失败: {e})"
            else:
                text = "(无内容)"
        samples.append({"path": path, "category": entry.get("category", ""),
                        "sample": text[:2500]})
    # 清单快照进 state：FileContext 不进 checkpoint，interrupt 恢复后 runtime.context 为空，
    # 入库阶段必须读 state 中的清单
    return {**state, "build_samples": samples, "build_manifest": manifest}


def build_db_design(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """建库流程·设计（体系B OODA 研究）：事实采集 → AI 设计 → 代码验证 → 修正

    不再是单次 LLM 调用。先由代码计算机读文件的列基数事实（distinct 统计），
    AI 基于事实+样本设计，代码再验证实体声明（1:N 拆分须有重复键列、
    单表设计须无强重复实体列），验证不过带结果让 AI 修正（限 2 轮）。
    """
    from agent.open_layer.schema_research import (
        compute_cardinality_facts, research_schema,
    )
    from pathlib import Path as _P

    llm = _g._get_llm(role="schema_design")  # schema 设计是推理密集步
    manifest, _ = _get_file_ctx(runtime)
    manifest = state.get("build_manifest") or manifest
    manifest_text = format_file_manifest(manifest)
    feedback = state.get("schema_feedback", "")

    # 提取用户原始输入（无文件样本时的设计依据 + 行业注册包生成依据）
    user_input = ""
    for m in reversed(state.get("messages", [])):
        mtype = getattr(m, "type", "") if not isinstance(m, dict) else m.get("type", "")
        if mtype == "human":
            c = getattr(m, "content", "") if not isinstance(m, dict) else m.get("content", "")
            user_input = c if isinstance(c, str) else extract_text_from_content(c)
            break

    # 观察：对机读文件计算列基数事实（xlsx/csv；PDF 等自动降级）
    facts_list = []
    for entry in manifest:
        sp = entry.get("server_path", "")
        if sp and _P(sp).exists():
            facts_list.append({"path": entry.get("path", sp),
                               "facts": compute_cardinality_facts(sp)})

    from core.llm_usage import set_role as _usage_role
    with _usage_role("schema_design"):
        schema = research_schema(manifest_text, state.get("build_samples", []),
                                 facts_list, feedback, llm, user_intent=user_input)

    # 新行业意图：行业注册包（名称/词典/路由样例）与 schema 同一次人工确认
    if state.get("industry_intent"):
        try:
            from agent.open_layer.industry_pack import gen_industry_pack
            schema["industry_pack"] = gen_industry_pack(llm, user_input, schema)
            logger.info("行业注册包已产出: %s",
                        schema["industry_pack"].get("config", {}).get("name"))
        except Exception as e:
            logger.warning("行业注册包生成失败（不阻断建表主流程）: %s", e)
    return {**state, "proposed_schema": schema}


def build_db_confirm(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """建库流程·人工确认：interrupt 暂停，等待用户批准/编辑/拒绝 schema

    前端 agent-inbox 渲染 action_requests/review_configs 标准格式，
    用户操作后以 {"decisions": [{type: approve|reject|edit, ...}]} 恢复。
    """
    from langgraph.types import interrupt

    schema = state.get("proposed_schema", {})
    tables = schema.get("tables", [])
    rounds = state.get("schema_confirm_rounds", 0)

    # 代码验证遗留问题（OODA 多轮仍未通过）→ 在确认卡片上明确警示，由人裁决
    v_issues = schema.get("_verification_issues") or []
    v_warn = ""
    if v_issues:
        v_warn = ("\n\n⚠️ 代码验证发现问题（请重点检查）：\n"
                  + "\n".join(f"- {i}" for i in v_issues))

    decision = interrupt({
        "action_requests": [{
            "name": "confirm_schema",
            "args": {"schema": schema},
            "description": (
                (f"新行业：{schema['industry_pack']['config'].get('name', '?')}"
                 f"（{schema['industry_pack']['config'].get('description', '')}）。"
                 if schema.get("industry_pack", {}).get("config") else "")
                + f"AI 设计了 {len(tables)} 张表：{', '.join(t.get('business_name') or t.get('name', '?') for t in tables)}。"
                f"设计理由：{schema.get('rationale', '无')}。"
                "请确认表结构和表关系是否合理——批准后自动建表并入库；"
                "拒绝请说明调整意见；也可直接编辑 schema。" + v_warn
            ),
        }],
        "review_configs": [{
            "action_name": "confirm_schema",
            "allowed_decisions": ["approve", "reject", "edit"],
        }],
    })

    d = {}
    if isinstance(decision, dict):
        decisions = decision.get("decisions") or []
        d = decisions[0] if decisions else {}
    dtype = d.get("type", "approve")

    if dtype == "edit":
        edited = d.get("edited_action", {}).get("args", {})
        if isinstance(edited.get("schema"), dict):
            return {**state, "proposed_schema": edited["schema"], "schema_feedback": ""}
        return {**state, "schema_feedback": ""}  # 编辑内容无效时按批准处理
    if dtype == "reject":
        return {**state,
                "schema_feedback": d.get("message", "") or "用户拒绝了该设计，请重新设计",
                "schema_confirm_rounds": rounds + 1}
    return {**state, "schema_feedback": ""}  # approve


def route_after_confirm(state: AgentState) -> str:
    """确认后路由：批准→建表；拒绝→带意见重新设计（不限轮数，直到用户确认为止）

    交互式建表原则：AI 提方案 → 用户提意见 → AI 修改 → 循环，直到批准。
    """
    if state.get("schema_feedback"):
        return "build_db_design"
    return "build_db_create"


def _switch_industry_local(name: str) -> None:
    """langgraph 进程内切换行业：与 mgmt 切换端点同一套公开重置入口

    只做本进程的状态切换（settings + 缓存单例）；mgmt 侧由调用方另行同步。
    全部走公开入口，不戳私有属性（U-9 状态卫生）。
    """
    settings.INDUSTRY = name
    try:
        from core.datasource_manager import DataSourceManager
        DataSourceManager.reset_instance()
        import core.data_ops as _data_ops
        _data_ops._federated_driver = None
    except Exception as e:
        logger.warning("行业切换：数据源缓存重置异常（继续）: %s", e)
    try:
        import industries.base as _base
        _base._industries.clear()
    except Exception:
        pass
    try:
        from core.graph.meta_db import MetaDB
        MetaDB.reset_instance()
    except Exception:
        pass
    try:
        from core.graph.schema_graph_service import SchemaGraphService
        SchemaGraphService.reset_instance()
    except Exception:
        pass
    try:
        from core.context import get_context
        get_context().clear_all()
    except Exception:
        pass


def build_db_create(state: AgentState, runtime: Runtime[FileContext] = None) -> AgentState:
    """建库流程·建表（第一步，到此为止）：按确认的 schema 建表

    架构约定（用户决策）：建表与录数据是两个独立步骤，互不影响。
    本节点只负责把表建好；数据录入由用户之后说"入库"触发，
    走标准的 导入→文件 通道（语义路由会按各表业务描述自动选对目标表），
    期间用户可在 schema-designer 检查/修改表结构，录入以最终表结构为准。
    """
    from agent.open_layer.executor import get_agent

    agent = get_agent()
    schema = state.get("proposed_schema", {})
    tables = schema.get("tables", [])
    results = list(state.get("results", []))
    switched_industry = ""

    # 新行业注册路径：行业配置是建表的副产品——写配置 → 进程内切换行业
    # → mgmt 同步，然后**继续往下在新行业里建表**（用户无需手动切换/二次建库）
    if schema.get("industry_pack"):
        try:
            from agent.open_layer.industry_pack import write_industry_pack
            ind_name, lint_errors = write_industry_pack(schema["industry_pack"], tables)
        except Exception as e:
            return {**state, "results": results + [
                f"行业注册失败: {e}",
                _build_db_fact_summary(None, [], [], abort_reason=f"行业注册失败: {e}"),
            ]}
        lint_note = "配置校验通过" if not lint_errors else \
            "配置校验提醒: " + "；".join(str(x) for x in lint_errors[:3])
        results.append(f"【行业注册】{ind_name}（{lint_note}）")

        _switch_industry_local(ind_name)
        switched_industry = ind_name
        # mgmt 同步（.env 持久化 + mgmt 侧缓存重置）；失败不阻断——本地已切换
        try:
            import urllib.request, json as _json
            req = urllib.request.Request(
                f"http://{settings.MGMT_HOST}:{settings.MGMT_PORT}/api/industries/switch",
                data=_json.dumps({"industry": ind_name}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-API-Key": settings.API_KEY},
                method="POST")
            urllib.request.urlopen(req, timeout=5).read()
            results.append(f"【行业切换】当前行业已切换为 {ind_name}")
        except Exception as e:
            logger.warning("mgmt 行业同步失败（本地已切换，稍后可手动同步）: %s", e)
            results.append(f"【行业切换】本地已切换为 {ind_name}（管理端同步失败，可在行业管理手动确认）")

    # definitions 格式与 batch_create_tables 工具一致
    definitions = []
    for t in tables:
        if not t.get("name") or not t.get("columns"):
            continue
        definitions.append({
            "name": t["name"],
            "business_name": t.get("business_name", ""),
            "description": t.get("description", ""),
            "columns": [
                {"name": c["name"], "type": c.get("type", "TEXT"),
                 "business_name": c.get("business_name", "")}
                for c in t["columns"] if c.get("name")
            ],
            "foreign_keys": t.get("foreign_keys", []),
        })
    if not definitions:
        return {**state, "results": results + [
            "建库中止：未获得有效表结构设计",
            _build_db_fact_summary(None, [], [], abort_reason="未获得有效表结构设计"),
        ]}

    create_res = agent.execute_single(
        "按用户确认的 schema 建表",
        behavior_key="增", db_category_key="表",
        structured_args={"definitions": definitions},
    )
    results.append(f"【建表】{create_res}")
    if _is_create_failure(create_res):
        results.append(_build_db_fact_summary(create_res, definitions, []))
        return {**state, "results": results}

    # 建表后同步元数据图层（YAML → MetaDB + Ladybug 图库）——
    # batch_create_tables 只写 YAML 与物理表，不同步画布/查询就读不到新表
    try:
        from core.graph.schema_graph_service import SchemaGraphService
        sync_res = SchemaGraphService.get_instance().sync_from_yaml()
        results.append(
            f"【元数据同步】{sync_res.get('synced', 0)} 张表已同步到元数据层"
            + (f"（{sync_res.get('errors', 0)} 个错误）" if sync_res.get("errors") else ""))
    except Exception as e:
        logger.warning("元数据同步失败（表已建，可在表设计页手动'同步 YAML'）: %s", e)

    # 建表完成：事实汇总 + 引导用户检查表结构后说"入库"
    results.append(_build_db_fact_summary(create_res, definitions, []))
    table_names = "、".join(d["name"] for d in definitions)
    industry_line = (
        f"新行业 '{switched_industry}' 已创建并切换为当前行业，表已建在其中。\n"
        if switched_industry else ""
    )
    results.append(
        f"✅ 建表完成（{table_names}）。\n" + industry_line +
        "接下来你可以：\n"
        "1. 在「表设计」页面检查或修改表结构和表关系（录入将以最终结构为准）\n"
        "2. 确认无误后，对我说「入库」或「把文件录入数据」，"
        "我会按最终表结构逐文件录入（表格进关系库，文字进向量库）"
    )
    return {**state, "results": results}
