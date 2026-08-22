"""树数据结构路由引擎（决策树数据外置：agent/decision_tree/ 目录多文件合并）

3.3 模块化：单 decision_tree.yaml 拆为按意图域的多文件（_root/query/insert/
update/delete/file/_shared），加载时合并为同一棵树——只改物理组织，
路由语义与拆分前完全一致（四项结构校验在合并后的整树上跑）。
"""

import yaml
from pathlib import Path
from config.settings import settings
from core.ai_runtime.ai_client import AIClient


# ═══════════════════════════════════════════════════════════════
# 决策树加载与校验（节点结构约定见 decision_tree/_root.yml 头部注释）
# ═══════════════════════════════════════════════════════════════

_TREE_DIR = Path(__file__).parent / "decision_tree"
_ROOT = "l1"
_DECISION_DIMS = ("behavior", "db", "constraint")


class DecisionTreeError(ValueError):
    """决策树配置（decision_tree/ 目录）加载/校验失败"""


def _normalize_nodes(raw, source):
    """把 YAML 原始结构规整为运行期节点表（m 列表 → set），并做字段级校验"""
    nodes = {}
    for nid, node in raw.items():
        loc = f"{source} 节点 {nid!r}"
        if not isinstance(node, dict):
            raise DecisionTreeError(f"{loc}: 节点必须是映射")
        if "tool" in node:
            extra = set(node) - {"tool"}
            if extra:
                raise DecisionTreeError(f"{loc}: 叶子节点含多余字段 {sorted(extra)}")
            if not isinstance(node["tool"], str) or not node["tool"]:
                raise DecisionTreeError(f"{loc}: tool 必须是非空字符串")
            nodes[nid] = {"tool": node["tool"]}
            continue
        missing = {"c", "m", "l", "r"} - set(node)
        extra = set(node) - {"c", "m", "l", "r"}
        if missing:
            raise DecisionTreeError(f"{loc}: 决策节点缺字段 {sorted(missing)}")
        if extra:
            raise DecisionTreeError(f"{loc}: 决策节点含多余字段 {sorted(extra)}")
        if node["c"] not in _DECISION_DIMS:
            raise DecisionTreeError(f"{loc}: c={node['c']!r} 非法，须为 {_DECISION_DIMS} 之一")
        m = node["m"]
        if isinstance(m, list):
            m = set(m)
        elif not isinstance(m, str):
            raise DecisionTreeError(f"{loc}: m 必须是字符串或字符串列表")
        nodes[nid] = {"c": node["c"], "m": m, "l": node["l"], "r": node["r"]}
    return nodes


def validate_tree(nodes, root=_ROOT, tool_names=None):
    """四项结构校验：根可达全部节点 / 无悬空引用 / 叶子工具已注册 / 假决策节点检测

    tool_names 为 None 时从工具注册表取（注册表未填充则先加载 agent.tools）。
    """
    if tool_names is None:
        from core.tool_registry import _tools
        if not _tools:
            import agent.tools  # noqa: F401  独立导入场景：确保工具已注册
        tool_names = _tools
    if root not in nodes:
        raise DecisionTreeError(f"根节点 {root!r} 不存在")
    # 1) 悬空引用：决策节点的 l/r 必须指向已定义节点
    for nid, node in nodes.items():
        if "tool" in node:
            continue
        for side in ("l", "r"):
            if node[side] not in nodes:
                raise DecisionTreeError(
                    f"节点 {nid!r} 的 {side} 分支指向不存在的节点 {node[side]!r}")
    # 2) 可达性：从根出发能到达全部节点
    seen, stack = set(), [root]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        node = nodes[nid]
        if "tool" not in node:
            stack += [node["l"], node["r"]]
    unreachable = sorted(set(nodes) - seen)
    if unreachable:
        raise DecisionTreeError(f"从根节点 {root!r} 不可达的节点: {unreachable}")
    # 3) 叶子工具必须已注册
    for nid, node in nodes.items():
        if "tool" in node and node["tool"] not in tool_names:
            raise DecisionTreeError(
                f"叶子节点 {nid!r} 的工具 {node['tool']!r} 未在工具注册表中注册")
    # 4) 假决策节点：l == r 假装覆盖——不是决策，应删节点并把父引用直连目标
    for nid, node in nodes.items():
        if "tool" in node:
            continue
        if node["l"] == node["r"]:
            raise DecisionTreeError(
                f"节点 {nid!r} 是假决策节点：l 与 r 同指 {node['l']!r}，"
                f"应删除该节点并把父引用直连目标")


def _load_nodes(tree_dir=_TREE_DIR):
    """加载 decision_tree/ 目录全部 *.yml 并合并为一棵树（3.3 多文件模块化）

    合并规则：节点 id 跨文件唯一（重复即配置错误，报错指明两个来源文件）；
    每文件单独 normalize（报错定位到文件），合并后整树跑四项结构校验。
    """
    tree_dir = Path(tree_dir)
    if not tree_dir.is_dir():
        raise DecisionTreeError(f"决策树配置目录不存在: {tree_dir}")
    files = sorted(tree_dir.glob("*.yml"))
    if not files:
        raise DecisionTreeError(f"决策树配置目录为空: {tree_dir}")
    merged, origin = {}, {}
    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise DecisionTreeError(f"决策树配置 YAML 解析失败: {f}: {e}") from None
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict):
            raise DecisionTreeError(f"{f}: 顶层缺少 nodes 映射")
        nodes = _normalize_nodes(data["nodes"], str(f))
        for nid in nodes:
            if nid in merged:
                raise DecisionTreeError(
                    f"节点 {nid!r} 重复定义: {f.name} 与 {origin[nid]}")
            origin[nid] = f.name
        merged.update(nodes)
    validate_tree(merged)
    return merged


_NODES = _load_nodes()


_CANONICAL_MAPS = None


def _canonical_maps() -> dict:
    """canonical 归一映射表（P2-3 两端词表统一）：行业别名 → canonical 键。

    来源：行业 terminology 的 behavior_aliases/object_aliases（行业知识进配置，
    不违反红线）。决策树 m 值集合内的词本身即可被树匹配，无需进表。
    """
    global _CANONICAL_MAPS
    if _CANONICAL_MAPS is not None:
        return _CANONICAL_MAPS
    behavior_map, object_map = {}, {}
    try:
        from industries.base import get_current_industry
        term = get_current_industry().terminology or {}
        for std, aliases in (term.get("behavior_aliases") or {}).items():
            for a in aliases or []:
                behavior_map.setdefault(a, std)
        for std, aliases in (term.get("object_aliases") or {}).items():
            for a in aliases or []:
                object_map.setdefault(a, std)
    except Exception:
        pass
    _CANONICAL_MAPS = {"behavior": behavior_map, "db": object_map}
    return _CANONICAL_MAPS


def canonicalize_intent(bk: str, dk: str, ct: str = ""):
    """意图标签归一到 canonical 键——决策树消费前的唯一归一点（P2-3）。

    parse_semantic（单步直连模式）与 decompose LLM（图模式）两端的输出
    都经此归一，词表漂移在入口被吸收。
    """
    maps = _canonical_maps()
    return maps["behavior"].get(bk, bk), maps["db"].get(dk, dk), ct


# 确定性行为关键词（文本铁证）：LLM 意图标签与指令文本冲突时以文本为准。
# 多字词优先，避免 "加快/加重" 这类误命中；"加一" 覆盖 "加一个字段" 句式。
_BEHAVIOR_KEYWORDS = (
    ("增", ("增加", "添加", "新增", "加一", "新建", "创建", "插入", "录入")),
    ("删", ("删除", "删掉", "去掉", "清空", "清除", "移除")),
    ("改", ("修改", "改成", "改为", "设置", "设为", "重命名")),
    ("查", ("查询", "查看", "列出", "显示", "统计", "有没有", "哪些")),
)


def text_db_override(text: str) -> str:
    """指令文本的确定性对象判定（文本铁证，LLM 标签与文本冲突时以文本为准）：

    '明细' + ('对应'/'主表'/'每条') → 关联（主细联查）。
    '每条主表记录对应的明细数据'这类话，LLM 标签常打成普通查表导致联查失败。

    '哪些表/几张表/有什么表' → 表。"数据库中有哪些表"这类话，LLM 常被打成
    数据库（list_databases 答非所问的真事故）；"表有哪些字段"不含"哪些表"，不误伤。
    """
    if "明细" in text and any(k in text for k in ("对应", "主表", "每条")):
        return "关联"
    if any(k in text for k in ("哪些表", "几张表", "多少张表", "多少个表", "有什么表", "有啥表")):
        return "表"
    return ""


def text_behavior_override(text: str) -> str:
    """指令文本的确定性行为判定：唯一命中行为族才返回该行为，否则返回空。

    多族命中（如"查询并删除"）属于真复合指令，不覆盖，交给 LLM 拆解。
    """
    hits = set()
    for bk, words in _BEHAVIOR_KEYWORDS:
        if any(w in text for w in words):
            hits.add(bk)
    return hits.pop() if len(hits) == 1 else ""


class _DecisionTree:
    """二叉树路由决策树——模块内部类，外部通过 get_tree() 获取唯一实例"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, 'nodes'):
            self.nodes = _NODES

    def trace_path(self, bk, dk, ct=""):
        bk, dk, ct = canonicalize_intent(bk, dk, ct)
        nid = "l1"
        path = []
        details = {}
        for _ in range(30):
            node = self.nodes.get(nid)
            if not node: return "query", path, details
            path.append(nid)
            if "tool" in node:
                details[nid] = {"type": "leaf", "tool": node["tool"]}
                return node["tool"], path, details
            val = {"behavior": bk, "db": dk, "constraint": ct}[node["c"]]
            m = node["m"]
            matched = val in m if isinstance(m, set) else val == m
            details[nid] = {"type": node["c"], "match": m, "value": val, "result": "left" if matched else "right"}
            nid = node["l"] if matched else node["r"]
        return "query", path, details

    def route(self, bk, dk, ct=""):
        tool, _, _ = self.trace_path(bk, dk, ct)
        return tool


_tree = None


def get_tree():
    global _tree
    if _tree is None:
        _tree = _DecisionTree()
    return _tree


def parse_semantic(text):
    """单步直连模式的意图解析（P2-3 定位：仅此模式使用）。

    图模式（LangGraph）的意图标签由 decompose LLM 一处生成并透传，
    不经过本函数。两端输出统一经 canonicalize_intent 归一后消费决策树。
    """
    ai = AIClient.get_instance()
    functions = [{
        "type": "function",
        "function": {
            "name": "parse",
            "description": "parse db command to structured fields",
            "parameters": {
                "type": "object",
                "properties": {
                    "behavior_key": {"type": "string", "description": "\u6539/\u67e5/\u589e/\u5220/\u5bfc\u5165/\u4e0a\u4f20/\u5bfc\u51fa", "enum": ["\u6539", "\u67e5", "\u589e", "\u5220", "\u5bfc\u5165", "\u4e0a\u4f20", "\u5bfc\u51fa"]},
                    "behavior_value": {"type": "string", "description": "user original verb"},
                    "constraint": {"type": "string", "description": "\u6279\u91cf/\u7b2cN\u9875/\u6240\u6709/\u524dN\u6761/\u6807\u51c6/\u81ea\u5b9a\u4e49/\u5355\u6761/\u975e\u7a7a"},
                    "db_category_key": {"type": "string", "description": "\u6a21\u677f/\u4f1a\u8bdd/\u6570\u636e\u5e93/\u8868/\u8bb0\u5f55/\u5b57\u6bb5/\u5916\u952e/\u7d22\u5f15/\u7c7b\u578b/\u7cbe\u5ea6/\u7ed3\u6784/\u6587\u4ef6/\u5173\u8054/\u7edf\u8ba1"},
                    "db_category_value": {"type": "string", "description": "user original noun"},
                },
                "required": ["behavior_key", "db_category_key"]
            },
        },
    }]
    try:
        from core.llm_usage import set_role as _usage_role
        with _usage_role("extract_param"):
            _, args = ai.call_function(functions, text,
                system_prompt=_build_router_prompt())
        result = {k: args.get(k, "").strip() for k in ["behavior_key","behavior_value","constraint","db_category_key","db_category_value"]}
        return result
    except Exception:
        return {}


def _build_router_prompt() -> str:
    """构建语义路由提示词——行业特定示例从配置注入

    通用部分（行为定义、对象大类、行为对象树、关键原则）保持不变，
    行业特定示例从 prompts.yml 的 router_examples 字段动态拼接。
    """
    # 通用部分（不依赖行业）
    base_prompt = (
        "你是一位数据库自然语言接口的语义解析器。你的任务：从用户指令中提取两个信息——"
        "1. behavior_key：用户想做什么（7选1）"
        "2. db_category_key：用户在什么对象上操作（15选1）。"
        "一、7种标准行为：改(修改、改成、改为、设置、设为、重命名)、查(查询、查看、列出、显示、有没有、哪些)、增(加、新增、添加、新建、创建、插入、录入)、删(删除、删掉、去掉、清空、清除、移除)、导入(导入、加载、读入)、上传(上传、传文件)、导出(导出、保存、另存为)。"
        "二、15种对象大类(按层级)：数据库(数据库、库、引擎、类型、文件路径)、模板(模板、表模板、结构模板)、会话(对话、历史、聊天记录、会话)、表(建表、创建表、新建表、删表、标准表)、记录(数据、记录、行、内容、字段值)、选择集(选择集、暂存数据、暂存、筛选结果)、结构(表结构、结构、字段列表、列信息、外键关系)、字段(加字段、删字段、字段、列)、外键(外键、FK、引用、关联、指向)、索引(索引、建索引、删除索引、唯一索引、普通索引)、类型(类型、数据类型、改为INTEGER、改为TEXT)、精度(精度、小数位、DECIMAL、精度设置)、关联(多表联合、JOIN、跨表查询、关联查询、连接查询、对比)、统计(统计、计数、求和、平均值、最大值、最小值、分组统计、聚合、总数)。"
        "三、行为对象树：改→记录(修改数据)；改→字段→主键(设为主键)|非空(设为非空)|其他(修改字段类型)；改→外键(改外键)；改→索引(改索引)；改→类型(修改字段类型)；改→精度(修改精度)；改→模板(模板操作)。查→数据库(查看数据库信息)；查→模板(列出模板)；查→表/结构/字段/外键/索引/类型/精度(查表结构)；查→记录(查询单表数据)；查→选择集(列出选择集)；查→关联(多表联合查询)；查→统计(聚合统计查询)。增→记录(插入数据)；增→表(建表)；增→字段(加字段/加外键/加索引)；增→模板(保存/导入模板)。删→记录(删除数据)；删→表(删表)；删→字段(删字段/删外键)；删→索引(删索引)；删→模板/会话(删模板/清除会话)。导入→文件(处理文件入库)；导入→模板(导入模板)。上传→文件(上传文件)。导出→记录(导出数据为CSV)；导出→模板(保存模板)。"
        "四、关键原则：1.选择集是表格数据被条件筛选后的数据集合。查选择集时它是对象大类(bk=查,dk=选择集)；修改选择集#N时它只是筛选条件(bk=改,dk=记录)。2.设为非空/设为索引→设是改行为，非空/索引才是对象；设为非空时constraint=非空。3.模糊动词没对象→默认dk=记录。4.只输出behavior_key和db_category_key。5.behavior_key只从7个里选，db_category_key只从15个里选。6.涉及多张表关联查询时选dk=关联。7.涉及计数/求和/平均值/分组统计时选dk=统计。8.问'有哪些表/几张表/有什么表'时选dk=表（即使句中出现'数据库'字样）；查→数据库只用于问数据库本身的信息（有哪些数据库、库的类型、路径）。"
        "五、通用示例：查询t1的所有数据→{查,记录}；查询选择集→{查,选择集}；修改选择集#5中a2的值为xxx→{改,记录}；设置t1的a1为非空→{改,字段}；设置t1的a1为索引→{改,索引}；新建表t2包含字段b1→{增,表}；删除索引idx_t1_a1→{删,索引}；有哪些数据库→{查,数据库}；把统计结果导出为CSV→{导出,记录}；导出查询结果→{导出,记录}；把数据导出成文件→{导出,记录}。"
    )
    # 行业特定示例从配置注入
    try:
        from industries.base import get_current_industry
        cfg = get_current_industry()
        if cfg.router_examples:
            industry_exs = "；".join(
                f'{ex["input"]}→{{{ex["behavior_key"]},{ex["db_category_key"]}}}'
                for ex in cfg.router_examples
            )
            base_prompt += f"六、行业示例：{industry_exs}。"
        # 从 tool_examples 补充关联/统计的判断提示
        if cfg.tool_examples:
            join_ex = cfg.tool_examples.get("join_query", "")
            agg_ex = cfg.tool_examples.get("aggregate_query", "")
            hints = []
            if join_ex:
                hints.append(f"涉及多张表关联查询时选dk=关联（如{join_ex}）")
            if agg_ex:
                hints.append(f"涉及计数/求和/平均值/分组统计时选dk=统计（如{agg_ex}）")
            if hints:
                base_prompt += "补充提示：" + "；".join(hints) + "。"
        # 术语映射注入——让 AI 把行业/个人表达映射到标准行为和标准对象
        # 注意：不改基本行为(7种)和基本对象(15种)，只提供"行业叫法→标准值"的翻译参考
        term = cfg.terminology or {}
        term_parts = []
        # 行为别名：行业表达 → 7种标准行为
        behavior_aliases = term.get("behavior_aliases", {})
        if behavior_aliases:
            ba_lines = []
            for std_key, aliases in behavior_aliases.items():
                if aliases:
                    ba_lines.append(f'"{",".join(aliases)}"→behavior_key={std_key}')
            if ba_lines:
                term_parts.append("行为映射：" + "；".join(ba_lines))
        # 对象别名：行业叫法 → 15种标准对象
        object_aliases = term.get("object_aliases", {})
        if object_aliases:
            oa_lines = []
            for std_key, aliases in object_aliases.items():
                if aliases:
                    oa_lines.append(f'"{",".join(aliases)}"→db_category_key={std_key}')
            if oa_lines:
                term_parts.append("对象映射：" + "；".join(oa_lines))
        # 表别名：业务叫法 → 标准表名
        table_aliases = term.get("table_aliases", {})
        if table_aliases:
            ta_lines = []
            for std_table, aliases in table_aliases.items():
                if aliases:
                    ta_lines.append(f'"{",".join(aliases)}"→表={std_table}')
            if ta_lines:
                term_parts.append("表名映射：" + "；".join(ta_lines))
        if term_parts:
            base_prompt += "七、术语映射（行业/个人表达→标准值，帮助理解用户意图）：" + "。".join(term_parts) + "。"
    except Exception:
        pass  # 配置加载失败时使用纯通用提示词
    return base_prompt



def route(user_input):
    ps = parse_semantic(user_input)
    bk = ps.get("behavior_key", "")
    dk = ps.get("db_category_key", "")
    ct = ps.get("constraint", "")
    # 文本铁证纠偏：与结构化标签路径同一白盒哲学（"数据库中有哪些表"被打成数据库的真事故）
    tbk = text_behavior_override(user_input)
    tdk = text_db_override(user_input)
    if tbk and tbk != bk:
        bk = tbk
    if tdk and tdk != dk:
        dk = tdk
    tool, path_info, _ = get_tree().trace_path(bk, dk, ct)
    parsed = {"behavior": bk, "db_category": dk, "constraint": ct, "_path": path_info,
              "behavior_value": ps.get("behavior_value",""), "db_category_value": ps.get("db_category_value","")}
    return tool, parsed



