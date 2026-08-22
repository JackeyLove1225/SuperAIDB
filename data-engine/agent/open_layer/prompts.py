"""Prompt 模板与拼装——开放式 AI 编排器的所有 prompt 定义集中在此

graph.py 只保留图逻辑（节点/路由/构建），prompt 文本统一通过本模块的 builder 函数获取。
所有动态插值点（行业配置、术语映射、few-shot 示例、文件清单、对话历史、结果截断）
的行为与原 graph.py 内联实现保持一致——本模块是纯搬家，不改任何 prompt 文案。

动态插值点清单：
- build_system_prompt: expert_role / hierarchy_section / examples_section / terminology_section
  （均来自当前行业配置 industries.base.get_current_industry()）
- build_decompose_prompt: instruction / collections / max_tasks / examples /
  join_example_hint / agg_example_hint / terminology_section
- build_simple_file_synthesis_prompt: user_input / single_result / manifest_text
- build_complex_synthesis_prompt: user_input / manifest_section / results_text /
  failure_hint / truncation_hint
"""

import json
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, AIMessage

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 上下文窗口管理——防止大结果集导致 LLM token 溢出
MAX_RESULT_CHARS = 2000       # 单个子任务结果最大字符数
MAX_TOTAL_RESULTS_CHARS = 8000  # 所有子任务结果汇总最大字符数

# 对话历史窗口与截断——单一事实源（20260805 上下文修复 A1）：
# agent 循环（消息对象形态）与 decompose（文本形态）共用同一套规则，
# 不再各搞一套（原 decompose 4 轮/200 字符截断，agent 循环零历史）。
HISTORY_MAX_TURNS = 8         # 最近 N 轮（1 轮 = 1 用户 + 1 助手）
HISTORY_AI_MAX_CHARS = 500    # 单条 AI 回复截断（保住表名/数字等关键信息）
HISTORY_USER_MAX_CHARS = 1000  # 单条用户消息截断（文件块已被占位符替换，此处兜底）

# 系统 Prompt——通用部分（行业特定内容从配置注入）
_SYSTEM_PROMPT_BASE = """{expert_role}。你可以帮助用户查询数据库、处理文件、管理表结构，以及理解文档内容。

你的核心能力：
1. 理解用户的复杂意图，拆解为多个可执行的子任务
2. 数据库操作子任务（type=db）：查询数据、管理表结构、处理文件入库、多表关联查询、聚合统计等，通过底层系统执行
3. 文档检索子任务（type=rag）：搜索已上传文档的文本内容，回答关于文档内容的问题
4. 综合所有子任务的结果，生成最终回复
5. 支持多轮对话：能理解上下文引用（如"再查一下"、"对比它们的"、"上面的"等）

重要规则：
- 你不能直接操作数据库，所有数据库操作都通过底层系统执行（type=db）
- 文档内容问题使用向量检索（type=rag），如"文档讲了什么"、"第X页的内容"、"什么是XXX"
- 子任务必须是一条清晰的指令
- 如果用户的问题只需要一步操作就能解决，直接返回该指令
- 如果需要多步操作，按顺序列出所有子任务
- 如果用户引用了上文（如"它的"、"上面的"），请结合对话历史理解具体指代

{hierarchy_section}{examples_section}{terminology_section}"""

# 任务拆解 Prompt——通用模板（行业特定示例从配置注入）
from agent.open_layer.prompt_templates import (
    DECOMPOSE_PROMPT_TEMPLATE as _DECOMPOSE_PROMPT_TEMPLATE,
    SCHEMA_DESIGN_TEMPLATE as _SCHEMA_DESIGN_TEMPLATE,
)


def _get_industry_cfg():
    """获取当前行业配置（带缓存）"""
    from industries.base import get_current_industry
    return get_current_industry()


def build_system_prompt() -> str:
    """构建系统提示词——行业特定内容从配置注入"""
    cfg = _get_industry_cfg()
    expert_role = cfg.expert_role or "你是一个智能数据助手"
    hierarchy_section = f"你的数据层级：\n{cfg.hierarchy_desc}\n" if cfg.hierarchy_desc else ""
    # 判断规则示例从 decompose_examples 前 4 条生成
    examples = cfg.decompose_examples[:4] if cfg.decompose_examples else []
    if examples:
        lines = []
        for ex in examples:
            complexity = "简单" if not ex.get("is_complex") else "复杂"
            steps = len(ex.get("sub_tasks", []))
            types = "/".join(set(t.get("type", "db") for t in ex.get("sub_tasks", [])))
            lines.append(f'- "{ex["query"]}" → {complexity}，{steps}步，type={types}')
        examples_section = "判断规则：\n" + "\n".join(lines)
    else:
        examples_section = ""
    # 术语映射注入——让 AI 理解行业/个人表达方式
    term = cfg.terminology or {}
    term_parts = []
    behavior_aliases = term.get("behavior_aliases", {})
    if behavior_aliases:
        ba = []
        for std_key, aliases in behavior_aliases.items():
            if aliases:
                ba.append(f"{std_key}←{','.join(aliases)}")
        if ba:
            term_parts.append("行为术语：" + "；".join(ba))
    object_aliases = term.get("object_aliases", {})
    if object_aliases:
        oa = []
        for std_key, aliases in object_aliases.items():
            if aliases:
                oa.append(f"{std_key}←{','.join(aliases)}")
        if oa:
            term_parts.append("对象术语：" + "；".join(oa))
    table_aliases = term.get("table_aliases", {})
    if table_aliases:
        ta = []
        for std_table, aliases in table_aliases.items():
            if aliases:
                ta.append(f"{std_table}←{','.join(aliases)}")
        if ta:
            term_parts.append("表名术语：" + "；".join(ta))
    terminology_section = "术语参考（用户表达→标准概念）：" + "。".join(term_parts) + "\n" if term_parts else ""
    return _SYSTEM_PROMPT_BASE.format(
        expert_role=expert_role,
        hierarchy_section=hierarchy_section,
        examples_section=examples_section,
        terminology_section=terminology_section,
    )


def build_decompose_prompt(instruction: str, collections: str, max_tasks: int,
                           tables_str: str = "", history_text: str = "",
                           manifest_text: str = "") -> str:
    """构建任务拆解提示词

    布局（B1 前缀缓存友好）：JSON 格式/规则等稳定段在模板顶部；
    表清单/术语/文档集合等低频变化段居中；对话历史/文件清单/用户指令
    等每轮变化内容统一沉底——任意一条新消息只让尾部 miss，
    前部稳定段持续命中 DeepSeek 前缀缓存。

    注：examples/join/agg hint 三个插值参数已于 20260805 移除——
    P2-2 模板搬家后模板内无对应占位符，属无效计算（few-shot 判断规则
    改由 build_system_prompt 注入前 4 条示例，行为无变化）。
    """
    cfg = _get_industry_cfg()
    # 术语映射段——让 LangGraph 知道 P1→树→P2 的术语体系
    term = cfg.terminology or {}
    term_parts = []
    # 行为别名
    behavior_aliases = term.get("behavior_aliases", {})
    if behavior_aliases:
        ba_lines = []
        for std_key, aliases in behavior_aliases.items():
            if aliases:
                ba_lines.append(f'"{",".join(aliases)}"→behavior_key={std_key}')
        if ba_lines:
            term_parts.append("行为映射：" + "；".join(ba_lines))
    # 对象别名
    object_aliases = term.get("object_aliases", {})
    if object_aliases:
        oa_lines = []
        for std_key, aliases in object_aliases.items():
            if aliases:
                oa_lines.append(f'"{",".join(aliases)}"→db_category_key={std_key}')
        if oa_lines:
            term_parts.append("对象映射：" + "；".join(oa_lines))
    # 表别名
    table_aliases = term.get("table_aliases", {})
    if table_aliases:
        ta_lines = []
        for std_table, aliases in table_aliases.items():
            if aliases:
                ta_lines.append(f'"{",".join(aliases)}"→表={std_table}')
        if ta_lines:
            term_parts.append("表名映射：" + "；".join(ta_lines))
    # router_examples 注入——让 LangGraph 知道什么样的表述对应什么标签
    if cfg.router_examples:
        re_lines = []
        for ex in cfg.router_examples:
            re_lines.append(f'{ex["input"]}→{{behavior_key={ex["behavior_key"]}, db_category_key={ex["db_category_key"]}}}')
        if re_lines:
            term_parts.append("路由示例：" + "；".join(re_lines))

    terminology_section = ""
    if term_parts:
        terminology_section = "术语映射（用于填写 behavior_key 和 db_category_key）：\n" + "。\n".join(term_parts)

    # 尾部段：每轮变化的内容统一沉底（历史在前、清单居中、指令最后）
    tail = ""
    if history_text:
        tail += f"对话历史（用于理解上下文引用）：\n{history_text}\n\n"
    if manifest_text:
        tail += manifest_text.rstrip("\n") + "\n\n"

    return _DECOMPOSE_PROMPT_TEMPLATE.format(
        instruction=instruction,
        collections=collections,
        max_tasks=max_tasks,
        tail_sections=tail,
        tables_section=tables_str or "（未提供，请先用 describe_schema 查看）",
        terminology_section=terminology_section,
    )


def truncate_result(text: str, max_chars: int = MAX_RESULT_CHARS) -> str:
    """截断过长的结果文本，保留首尾并提示省略部分

    采用"头尾保留"策略：保留前 60% 和后 30%，中间用省略标记连接。
    这样既能看到字段名/表头，又能保留结尾的汇总信息。
    """
    if not text or len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.6)
    tail_len = int(max_chars * 0.3)
    omitted = len(text) - head_len - tail_len
    return (
        text[:head_len]
        + f"\n...（已省略约 {omitted} 字符，可通过导出查看完整数据）...\n"
        + text[-tail_len:]
    )


def extract_text_from_content(content) -> str:
    """从消息内容中提取纯文本（处理多模态 content 列表）

    问题6修复：当用户上传文件/文件夹时，消息 content 是一个内容块列表（多模态），
    而非纯字符串。直接将列表塞入 prompt 会产生垃圾文本，且 base64 编码的二进制
    文件数据会撑爆 LLM token 上限导致 API 报错。

    问题4+5修复（防御性过滤）：前端将文件清单与内容分离后，text 块可能携带：
    - isFileManifest=true：文件清单块（清单已通过 runtime.context 注入 prompt，此处跳过）
    - isFileUpload=true：文本类文件上传块（内容已通过 runtime.context.file_contents 按需读取，
      此处替换为占位符，防止 LLM 被海量文本淹没）
    即使前端剥离失败，本函数也能保护 LLM 不被文件内容淹没。

    本函数将内容块列表转为干净的文本：
    - text 块（isFileManifest）→ 跳过（清单已通过 runtime 注入）
    - text 块（isFileUpload）→ 替换为 "[文件: path]" 占位符（不读 text 字段）
    - text 块（普通）→ 直接使用 text 字段
    - image 块：替换为 "[图片]" 占位符（不含 base64 数据）
    - file 块：替换为 "[文件: filename (mimeType)]" 占位符（不含 base64 数据）

    防溢出：单个 text 块超过 5000 字符时截断，总文本超过 20000 字符时截断。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        # 意外类型，安全转为字符串
        return str(content) if content is not None else ""

    parts = []
    total_len = 0
    MAX_PER_BLOCK = 5000
    MAX_TOTAL = 20000
    for block in content:
        if isinstance(block, str):
            text = block
        elif isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                meta = block.get("metadata", {}) or {}
                # 防御性过滤：文件清单块（已通过 runtime.context 注入）→ 跳过
                if meta.get("isFileManifest"):
                    continue
                # 防御性过滤：文件上传块（内容已通过 runtime.context.file_contents 按需读取）→ 占位符
                if meta.get("isFileUpload"):
                    path = (meta.get("relativePath") or meta.get("filename")
                            or meta.get("name") or "unknown")
                    parts.append(f"[文件: {path}]")
                    continue
                text = block.get("text", "")
            elif btype == "image":
                mime = block.get("mimeType", "image")
                parts.append(f"[图片: {mime}]")
                continue
            elif btype == "file":
                mime = block.get("mimeType", "file")
                meta = block.get("metadata", {}) or {}
                fname = (meta.get("filename") or meta.get("name")
                         or meta.get("relativePath") or "")
                if fname:
                    parts.append(f"[文件: {fname} ({mime})]")
                else:
                    parts.append(f"[文件: {mime}]")
                continue
            elif btype == "image_url":
                parts.append("[图片]")
                continue
            else:
                parts.append(f"[{btype}]")
                continue
        else:
            continue

        # 截断过长的 text 块
        if len(text) > MAX_PER_BLOCK:
            text = text[:MAX_PER_BLOCK] + "\n...（文件内容过长，已截断）..."
        parts.append(text)
        total_len += len(text)
        if total_len >= MAX_TOTAL:
            parts.append("...（上传内容过多，已截断，AI 仅能看到部分文件内容）...")
            break

    return "\n".join(parts) if parts else ""


def extract_latest_user_input(messages) -> str:
    """从消息列表中提取最新一条用户消息的纯文本（处理多模态 content）"""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            # 问题6：多模态消息 content 可能是列表（含上传文件），需提取纯文本
            return extract_text_from_content(msg.content)
        elif isinstance(msg, dict) and msg.get("role") == "user":
            return extract_text_from_content(msg.get("content", ""))
    return ""


def _iter_history_pairs(messages):
    """提取 (role, text) 序列：只保留 user/ai 文本消息

    ToolMessage 与带 tool_calls 的 AI 消息是智能体循环的内部轨迹，
    注入会让 LLM 被上一轮的工具编排干扰（甚至模仿出幻觉调用），一律剔除。
    注意：本函数只做筛选与定长截断，不改写内容——消息原文逐轮稳定是
    DeepSeek 前缀缓存命中的前提（缓存按字节级前缀匹配）。
    """
    pairs = []
    for msg in messages or []:
        if isinstance(msg, HumanMessage):
            role, content = "user", msg.content
        elif isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                continue
            role, content = "assistant", msg.content
        elif isinstance(msg, dict):
            role = msg.get("role", "")
            if role not in ("user", "assistant") or msg.get("tool_calls"):
                continue
            content = msg.get("content", "")
        else:
            continue
        text = extract_text_from_content(content).strip()
        if not text:
            continue
        limit = HISTORY_USER_MAX_CHARS if role == "user" else HISTORY_AI_MAX_CHARS
        if len(text) > limit:
            text = text[:limit] + "..."
        pairs.append((role, text))
    return pairs


def _history_window(messages, max_turns: int):
    """窗口化：排除最新一条用户消息（当前指令由调用方单独传入），取最近 N 轮"""
    pairs = _iter_history_pairs(messages)
    if pairs and pairs[-1][0] == "user":
        pairs = pairs[:-1]
    return pairs[-(max_turns * 2):]


def build_chat_history(messages, max_turns: int = HISTORY_MAX_TURNS) -> list:
    """统一历史组装器（消息对象形态）——agent 循环的上下文来源（元凶1修复）

    返回真实 HumanMessage/AIMessage 对象列表，role 结构保留，
    指代消解（"再查一下"/"把它删掉"）远强于拼文本。消息原样进入下一轮
    请求（只追加不改写），前缀缓存天然命中。
    """
    return [HumanMessage(content=t) if r == "user" else AIMessage(content=t)
            for r, t in _history_window(messages, max_turns)]


def format_conversation_history(messages, max_turns: int = HISTORY_MAX_TURNS) -> str:
    """统一历史组装器（文本形态）——decompose prompt 注入用

    与 build_chat_history 同一窗口/截断规则（A1 单一事实源）；
    无历史时返回空字符串（调用方据此决定是否追加历史段落）。
    """
    pairs = _history_window(messages, max_turns)
    return "\n".join(f"{'用户' if r == 'user' else '助手'}: {t}"
                     for r, t in pairs) if pairs else ""


def format_file_manifest(manifest) -> str:
    """将文件清单（runtime.context.file_manifest）格式化为紧凑文本，注入 decompose prompt

    清单只含元信息（路径/类别/语言/大小），不含文件内容，体积小。
    AI 看到清单后可判断：
    - 用户问"有哪些文件"→ 直接基于清单回答，不生成 file_query
    - 用户问"分析某文件"→ 生成 type=file_query 子任务按需读取内容
    """
    if not manifest:
        return ""
    lines = ["📂 工作区已加载以下文件（如需查看某文件内容，生成 type=file_query 子任务）："]
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", entry.get("filename", ""))
        if not path:
            continue
        category = entry.get("category", "other")
        language = entry.get("language", "")
        size = entry.get("size", 0)
        if size > 1024:
            size_str = f"{size / 1024:.1f}KB"
        elif size > 0:
            size_str = f"{size}B"
        else:
            size_str = ""
        cat_lang = category + (f" [{language}]" if language else "")
        suffix = f" ({cat_lang}, {size_str})" if size_str else f" ({cat_lang})"
        server_path = entry.get("server_path", "")
        sp_note = f" → 服务器路径: {server_path}" if server_path else ""
        lines.append(f"- {path}{suffix}{sp_note}")
    lines[0] = ("📂 工作区已加载以下文件（如需查看某文件内容，生成 type=file_query 子任务；"
                "如需入库，process_file 的 filepath 使用清单中的服务器路径）：")
    return "\n".join(lines)


def build_truncated_results(results: list[str]) -> tuple[str, bool]:
    """构建截断后的结果汇总文本

    Returns:
        (results_text, has_truncated): 汇总文本和是否有截断
    """
    truncated_items = []
    total_chars = 0
    has_truncated = False

    for i, r in enumerate(results):
        # 逐条截断
        item = truncate_result(r, MAX_RESULT_CHARS)
        if len(item) < len(r):
            has_truncated = True

        # 检查总长度是否超限
        item_text = f"子任务{i+1}结果：\n{item}"
        if total_chars + len(item_text) > MAX_TOTAL_RESULTS_CHARS:
            # 总长度超限，进一步压缩
            remaining = MAX_TOTAL_RESULTS_CHARS - total_chars
            if remaining < 200:
                # 剩余空间太小，直接标记省略
                truncated_items.append(f"子任务{i+1}结果：（因总长度限制已省略）")
                has_truncated = True
                break
            item = truncate_result(r, remaining - 50)
            has_truncated = True
            item_text = f"子任务{i+1}结果：\n{item}"

        truncated_items.append(item_text)
        total_chars += len(item_text)

    return "\n\n".join(truncated_items), has_truncated


def build_error_summary(failed_tasks) -> str:
    """构建失败任务提示文本（追加到最终回复末尾），无失败时返回空字符串"""
    if not failed_tasks:
        return ""
    error_lines = []
    for ft in failed_tasks:
        error_lines.append(f"- 子任务「{ft['query'][:50]}」执行失败：{ft['error'][:100]}")
    return "\n\n⚠️ 以下子任务执行失败：\n" + "\n".join(error_lines)


def build_simple_file_synthesis_prompt(user_input: str, single_result: str, manifest_text: str) -> str:
    """简单任务 file_query 失败时的综合 prompt——基于文件清单直接回答"""
    return f"""用户指令：{user_input}

子任务执行结果（可能失败）：
{single_result}

{manifest_text}

请基于上方"工作区已加载文件"清单，直接回答用户的问题。
如果用户问"有哪些文件"，请列出清单中的文件名（可附上类别和大小）。
不要再说"未找到文件"，因为文件清单已经提供在上面了。"""


def build_complex_synthesis_prompt(
    user_input: str,
    results_text: str,
    has_failures: bool,
    has_truncated: bool,
    manifest_section: str = "",
) -> str:
    """复杂任务的综合 prompt——汇总所有子任务结果，附失败/截断提示"""
    # 如果有失败任务，在 prompt 中提示 LLM
    failure_hint = ""
    if has_failures:
        failure_hint = "\n\n注意：部分子任务执行失败，请在回复中说明哪些操作未能完成，并提供可能的原因和建议。"

    truncation_hint = ""
    if has_truncated:
        truncation_hint = "\n\n注意：部分子任务结果较长已被截断，请基于已展示的内容作答，并提示用户可使用导出功能查看完整数据。"

    return f"""用户指令：{user_input}

{manifest_section}已执行以下子任务并获得结果：

{results_text}
{failure_hint}{truncation_hint}

请综合以上结果，回答用户的问题。如果结果是表格数据，请整理为清晰的对比或汇总。"""


# ═══════════════════════════════════════════════════════════════
# 全自动建库流程（build_db）——schema 设计 prompt
# ═══════════════════════════════════════════════════════════════



def build_schema_design_prompt(manifest_text: str, samples: list, feedback: str = "") -> str:
    """构建 schema 设计 prompt——建库流程的"设计"阶段

    Args:
        manifest_text: 文件清单文本（format_file_manifest 的输出）
        samples: 探索阶段读取的文件样本 [{path, category, sample}]
        feedback: 人工确认环节拒绝时给出的调整意见（重新设计时非空）
    """
    sample_parts = []
    for s in samples:
        sample_parts.append(
            f"--- {s.get('path', '?')} ({s.get('category', '')}) ---\n{s.get('sample', '')[:2500]}"
        )
    samples_text = "\n\n".join(sample_parts) if sample_parts else "（无可用样本）"

    feedback_section = ""
    if feedback:
        feedback_section = (
            f"\n用户对上一版设计的不满意之处（请据此调整）：\n{feedback}\n"
        )

    return _SCHEMA_DESIGN_TEMPLATE.format(
        manifest_text=manifest_text or "（无）",
        samples_text=samples_text,
        feedback_section=feedback_section,
    )
