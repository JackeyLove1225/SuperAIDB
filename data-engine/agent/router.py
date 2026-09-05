"""树数据结构路由引擎（决策树数据外置：agent/decision_tree/ 目录多文件合并）

定位（20260824 硬路由）：**全部自然语言流量的唯一路由内核**——
- 主路由：agent/tools/instruct.py 的 execute_instruction（唯一 NL 通道），
  33 叶全部生产可达（不再是"仅 2 叶有消费"的资产态）
- 内嵌路由：core.data_ops.mutate_natural 的工具路由（删/改+记录 →
  delete_data/edit_data，经 bootstrap 注入的 _route_via_tree）
- 参数级确定性校验在下游 core/tool_arg_guard（execute_tool 边界闸）；
  写域链尾全部 fail-closed 到 unsupported_op（空/未知对象不落写工具）

3.3 模块化：单 decision_tree.yaml 拆为按意图域的多文件（_root/query/insert/
update/delete/file/_shared），加载时合并为同一棵树——只改物理组织，
路由语义与拆分前完全一致（五项结构校验在合并后的整树上跑）。
"""

import yaml
from pathlib import Path


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
    """五项结构校验：根可达全部节点 / 无悬空引用 / 叶子工具已注册 / 假决策节点 / 无环（DAG）

    tool_names 为 None 时从工具注册表取（注册表未填充则先加载 agent.tools）。
    """
    if tool_names is None:
        from core.tool_registry import get_tools
        tool_names = get_tools()
        if not tool_names:
            import agent.tools  # noqa: F401  独立导入场景：确保工具已注册
            tool_names = get_tools()
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
    # 5) 无环（DAG）：环会让 trace_path 原地打转——三色 DFS 抓回边
    #（决策节点出度≤2、叶子出度 0；前序校验已保证 l/r 指向存在）
    _WHITE, _GRAY, _BLACK = 0, 1, 2
    color = {nid: _WHITE for nid in nodes}
    for start in nodes:
        if color[start] != _WHITE:
            continue
        stack = [(start, iter(() if "tool" in nodes[start]
                            else (nodes[start]["l"], nodes[start]["r"])))]
        color[start] = _GRAY
        while stack:
            nid, it = stack[-1]
            advanced = False
            for child in it:
                if color[child] == _GRAY:
                    raise DecisionTreeError(
                        f"树存在环：{nid!r} → {child!r}（决策树必须是有向无环图，"
                        f"环会让路由无限递归）")
                if color[child] == _WHITE:
                    color[child] = _GRAY
                    cnode = nodes[child]
                    stack.append((child, iter(() if "tool" in cnode
                                              else (cnode["l"], cnode["r"]))))
                    advanced = True
                    break
            if not advanced:
                color[nid] = _BLACK
                stack.pop()


def _load_nodes(tree_dir=_TREE_DIR):
    """加载 decision_tree/ 目录全部 *.yml 并合并为一棵树（3.3 多文件模块化）

    合并规则：节点 id 跨文件唯一（重复即配置错误，报错指明两个来源文件）；
    每文件单独 normalize（报错定位到文件），合并后整树跑五项结构校验。
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
_CANONICAL_SIG = ""


def _canonical_maps() -> dict:
    """canonical 归一映射表（两端词表统一）：行业别名 → canonical 键。

    来源：行业 terminology 的 behavior_aliases/object_aliases（行业知识进配置，
    不违反红线）。决策树 m 值集合内的词本身即可被树匹配，无需进表。
    新鲜度：按行业目录签名失效——自学习写入 prompts.yml 后
    本进程下次调用即吃新别名（与"目录签名新鲜度免重启"口径一致，不永久缓存）。
    """
    global _CANONICAL_MAPS, _CANONICAL_SIG
    try:
        from industries.base import get_current_industry, _dir_mtime
        cfg = get_current_industry()
        sig = str(_dir_mtime(cfg.base_dir))
    except Exception:
        sig = ""
    if _CANONICAL_MAPS is not None and sig == _CANONICAL_SIG:
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
        pass  # 别名映射读失败则该别名缺席（主名匹配不受影响）
    _CANONICAL_MAPS = {"behavior": behavior_map, "db": object_map}
    _CANONICAL_SIG = sig
    return _CANONICAL_MAPS


def canonicalize_intent(bk: str, dk: str, ct: str = ""):
    """意图标签归一到 canonical 键——决策树消费前的唯一归一点。

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
    数据库（list_databases 会答非所问）；"表有哪些字段"不含"哪些表"，不误伤。
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
    """二叉树路由决策树——模块内部类，唯一实例由 get_tree() 持有（不再双套单例）"""

    def __init__(self):
        self.nodes = _NODES

    def trace_path(self, bk: str, dk: str, ct: str = "") -> tuple:
        bk, dk, ct = canonicalize_intent(bk, dk, ct)
        nid = "l1"
        path = []
        details = {}
        for _ in range(30):
            node = self.nodes.get(nid)
            # 结构异常（悬空/环）统一落 unsupported_op 如实报——加载期五项校验
            # 已挡死这两类，此处是纵深防线：绝不静默路由到 query（读工具）冒充成功
            if not node: return "unsupported_op", path, details
            path.append(nid)
            if "tool" in node:
                details[nid] = {"type": "leaf", "tool": node["tool"]}
                return node["tool"], path, details
            val = {"behavior": bk, "db": dk, "constraint": ct}[node["c"]]
            m = node["m"]
            matched = val in m if isinstance(m, set) else val == m
            details[nid] = {"type": node["c"], "match": m, "value": val, "result": "left" if matched else "right"}
            nid = node["l"] if matched else node["r"]
        return "unsupported_op", path, details

    def route(self, bk: str, dk: str, ct: str = "") -> str:
        tool, _, _ = self.trace_path(bk, dk, ct)
        return tool


_tree = None


def get_tree() -> _DecisionTree:
    global _tree
    if _tree is None:
        _tree = _DecisionTree()
    return _tree

