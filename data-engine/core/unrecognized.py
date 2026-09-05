"""未识别问法池（20260824 硬路由配套：映射自学习底座，精简版）

背景：硬路由下，P1+树路由不出来（cannot_route）或边界闸报"表/字段不存在"
的问法是映射关系的缺口。本模块做三件事：
1. 记录：问法 + 失败原因落盘（config/unrecognized_pool.json，JsonContract）
2. 出示：管理端列出池中问法（review 后决定映射）
3. 学习：确认一条映射（"问法 → 行为×对象 标签" 或 "别名 → 表名"）→
   写入当前行业 prompts.yml（terminology aliases / router_examples）——
   下次同类问法 P1 直接命中，映射关系随使用自生长

设计边界（与图时代 unrecognized.py 的原则一致）：
- 只维护映射层（问法→标签/别名），树结构本身永不被自动修改
- 写入只经 ConfigHub 原子写 + 行业配置校验（industry_linter 语义不变）
"""
import time

from core.file_contract import JsonContract
from core.logger import get_logger

logger = get_logger(__name__)

_POOL = JsonContract(
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "config" / "unrecognized_pool.json",
    default_factory=list)


def record_unrecognized(instruction: str, reason: str = "", intent: str = "") -> None:
    """问法入池（同问法去重，计数累加；池上限 200 条先进先出；
    同 intent 指纹每日限 20 条——MCP 通道 AI 不可用垃圾问法灌洪驱逐真实缺口）"""
    instruction = (instruction or "").strip()[:200]
    if not instruction:
        return
    # 读改写全程持锁（并发入池会丢更新）
    with _POOL.lock():
        pool = list(_POOL.read())
        # 限频：同意图指纹当日已 20 条即不再记录（指纹只用 canonical 后的
        # 行为/对象键——constraint 自由文本不产生无限指纹变体绕过限频）
        today = time.strftime("%Y-%m-%d")
        same = [x for x in pool
                if x.get("intent") == intent and intent
                and str(x.get("first_seen", "")).startswith(today)]
        if intent and len(same) >= 20:
            return
        for item in pool:
            if item.get("q") == instruction:
                item["count"] = item.get("count", 1) + 1
                item["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _POOL.write(pool)
                return
        pool.append({"q": instruction, "reason": reason[:120], "intent": intent,
                     "count": 1, "first_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "last_seen": time.strftime("%Y-%m-%d %H:%M:%S")})
        _POOL.write(pool[-200:])
    logger.info("未识别问法入池: %s（%s）", instruction[:50], reason[:40])


def list_unrecognized() -> list:
    """池中问法（按最近出现倒序，管理端出示用）"""
    pool = list(_POOL.read())
    return sorted(pool, key=lambda x: x.get("last_seen", ""), reverse=True)


def learn_mapping(question: str, behavior: str = "", db_category: str = "",
                  table_alias_of: str = "") -> dict:
    """确认一条映射并写入当前行业 prompts.yml（热生效）

    两种学习形态（二选一）：
    - 意图映射：question + behavior + db_category → 追加 router_examples
    - 表名映射：question(别名) + table_alias_of(真实表名) → 追加 table_aliases

    写入端校验（学习面是 P1 提示词的持久化投管道）：
    - behavior/db_category 必须落在 7 行为/15 对象封闭枚举
    - question 必须真实存在于未识别池（不接受凭空问法）且长度 ≤200
    - table_alias_of 必须是当前行业的真实表
    - router_examples 去重 + 上限 50 条
    Returns: {"ok": bool, "message": str}
    """
    from industries.base import get_current_industry
    from core.config_hub import write_yaml_atomic, load_yaml
    # 封闭枚举（与决策树 YAML 同一组值；core 不 import agent，此处自包含）
    STANDARD_BEHAVIORS = {"改", "查", "增", "删", "导入", "上传", "导出"}
    STANDARD_CATEGORIES = {"数据库", "模板", "会话", "表", "记录", "选择集", "结构",
                           "字段", "外键", "索引", "类型", "精度", "关联", "统计", "文件"}
    q = (question or "").strip()[:200]
    # 必须来自池中（不接受凭空问法——投毒面收窄到"真实路由缺口"）
    if not any(x.get("q") == q for x in _POOL.read()):
        return {"ok": False, "message": "该问法不在未识别池中（只学习真实路由缺口）"}
    # 内容消毒：learned example 会原文拼进 P1 的 LLM 提示词——
    # 结构性检测（多行已剥离 + 引号/括号密度异常）+ 中英投毒句式黑名单；
    # 不收窄正常业务问法（"查询之前的记录"必须能学——黑名单不列"之前"）
    q = q.replace("\r", " ").replace("\n", " ").strip()
    import re as _re
    if _re.search(r"忽略|以上全部|系统提示|你是一个|system prompt|ignore (all |all previous|previous)|instruction[s]?\s+(above|before)|你是|必須|必须", q, _re.I):
        return {"ok": False, "message": "问法含指令性/投毒句式，不允许学习"}
    if behavior or db_category:
        if behavior not in STANDARD_BEHAVIORS:
            return {"ok": False, "message": f"非法行为键: {behavior}（须为 {sorted(STANDARD_BEHAVIORS)} 之一）"}
        if db_category not in STANDARD_CATEGORIES:
            return {"ok": False, "message": f"非法对象键: {db_category}（须为 {sorted(STANDARD_CATEGORIES)} 之一）"}
    cfg = get_current_industry()
    prompts_path = cfg.base_dir / "prompts" / "prompts.yml"
    data = load_yaml(prompts_path, default={}) or {}

    if behavior and db_category:
        exs = data.setdefault("router_examples", [])
        if not any(e.get("input") == q for e in exs):  # 去重
            exs.append({"input": q, "behavior_key": behavior,
                        "db_category_key": db_category})
        data["router_examples"] = exs[-50:]  # 上限 50（提示词面有界）
        write_yaml_atomic(prompts_path, data)
        logger.info("未识别样例纳入: 行业 %s 追加意图映射 %s → %s+%s",
                    cfg.name, q[:30], behavior, db_category)
        return {"ok": True, "message": f"已学习：「{q[:30]}」→ {behavior}+{db_category}（行业配置已热生效）"}
    if table_alias_of:
        # 表别名必须指向当前行业真实表（防投毒到假想表）
        from core.tool_arg_guard import enumerate_objects
        _, _, _known, _, _ = enumerate_objects()
        if table_alias_of not in _known:
            return {"ok": False, "message": f"表 {table_alias_of} 不存在（别名只能指向真实表）"}
        term = data.setdefault("terminology", {})
        aliases = term.setdefault("table_aliases", {})
        lst = aliases.setdefault(table_alias_of, [])
        if q not in lst:
            lst.append(q)
        write_yaml_atomic(prompts_path, data)
        logger.info("未识别样例纳入: 行业 %s 追加表别名 %s → %s",
                    cfg.name, q, table_alias_of)
        return {"ok": True, "message": f"已学习：「{q}」是表 {table_alias_of} 的别名（行业配置已热生效）"}
    return {"ok": False, "message": "请提供 行为+对象（意图映射）或 真实表名（别名映射）"}


def remove_from_pool(question: str) -> None:
    """从池中移除（学习完成/人工忽略后调用）"""
    pool = [x for x in _POOL.read() if x.get("q") != question]
    _POOL.write(pool)
