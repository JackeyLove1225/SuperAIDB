"""记录写域——记录级增/改/删工具 handler。

insert_data / batch_insert_data / edit_data / delete_data / mutate_data。
"""
import json as _json

from core.tool_result import ToolResult


def _set_fields_of(set_data) -> list:
    """从 set_data（dict 或 "k = v, k2 = v2" 串）提取修改字段名（effects 用）。
    字符串形态走全仓唯一 SET 解析器（引号感知，值内逗号/等号不误切）。"""
    if isinstance(set_data, dict):
        return list(set_data.keys())
    from core.contract.security_contract import split_set_pairs
    return [col for col, _ in split_set_pairs(str(set_data)) if col]


def insert_data(table="", data="", database=""):
    if not table: return ToolResult.fail("请指定表名", code="VALIDATION", reason="missing_params")
    if not data: return ToolResult.fail("请指定数据内容", code="VALIDATION", reason="missing_params")
    try: row = _json.loads(data) if isinstance(data,str) else data
    except: return ToolResult.fail("data 格式错误，请使用 JSON 格式", code="VALIDATION", reason="data_format")
    if any(k.lower()=="id" for k in row.keys()):
        return ToolResult.fail("id 是系统主键，由系统自动生成，不允许手动指定",
                               code="VALIDATION", reason="primary_key")
    from core.data_ops import insert_row
    return insert_row(table, _json.dumps(row,ensure_ascii=False))


def batch_insert_data(table="", data="", database=""):
    if not table: return ToolResult.fail("请指定表名", code="VALIDATION", reason="missing_params")
    if not data: return ToolResult.fail("请指定批量数据内容", code="VALIDATION", reason="missing_params")
    try: rows = _json.loads(data) if isinstance(data,str) else data
    except: return ToolResult.fail("data 格式错误，请使用 JSON 格式", code="VALIDATION", reason="data_format")
    if not isinstance(rows,list): rows = [rows]
    results=[]
    from core.data_ops import insert_row
    skipped_id = 0
    for row in rows:
        if any(k.lower()=="id" for k in row.keys()):
            skipped_id += 1
            continue
        r=insert_row(table, _json.dumps(row,ensure_ascii=False))
        results.append(r)
    if not results:
        return ToolResult.fail("无有效数据", code="VALIDATION", reason="no_valid_rows",
                               table=table, skipped_id_rows=skipped_id)
    texts = [str(r) for r in results]
    n_ok = sum(1 for r in results if r.data.get("ok"))
    ok = n_ok == len(results)
    return ToolResult("; ".join(texts) if texts else "无有效数据", {
        "ok": ok, "code": "OK" if ok else "UNKNOWN",
        "table": table, "action": "INSERT",
        "affected": n_ok, "total": len(results),
        "skipped_id_rows": skipped_id,
        "effects": {"table": table, "action": "INSERT", "affected": n_ok,
                    "values": rows[:100]},
    })


def edit_data(table="", column="", database="", selection_id=0, set_data=""):
    from core.context import get_context
    if not selection_id:
        # 合理默认值：用最近选择集（与 database 默认第一个、table 从选择集默认一致）
        last_id = get_context().get_last_selection_id()
        if last_id:
            selection_id = last_id
        else:
            return ToolResult.fail("请先查询数据创建选择集", code="VALIDATION",
                                   reason="need_selection")
    sel = get_context().get_selection(selection_id)
    if not sel:
        return ToolResult.fail("选择集不存在", code="NOT_FOUND",
                               reason="selection_not_found")
    table = table or sel["table"]
    from core.contract.security_contract import ids_in_clause
    where = ids_in_clause(sel['ids'])
    if not set_data:
        return ToolResult.fail("请指定要修改的内容（如 a2=888）", code="VALIDATION",
                               reason="missing_params")
    # set_data 归一：开放层 AI 常传 JSON 对象（{"col": val}），
    # 而 update_rows/契约层只认 SQL 赋值串（col = val）——边界处统一转换
    import json as _json
    changed_fields = _set_fields_of(set_data)
    expected_values = None  # dict 形态的 set_data 保留期望值，供 goal_verify 字段对账
    def _to_set_clause(d: dict) -> str:
        def _lit(v):
            if isinstance(v, str):
                return "'" + v.replace("'", "''") + "'"
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "1" if v else "0"
            return str(v)
        return ", ".join(f"{k} = {_lit(v)}" for k, v in d.items())
    if isinstance(set_data, dict):
        expected_values = dict(set_data)
        set_data = _to_set_clause(set_data)
    elif isinstance(set_data, str) and set_data.strip().startswith("{"):
        try:
            _d = _json.loads(set_data)
            if isinstance(_d, dict):
                expected_values = dict(_d)
                set_data = _to_set_clause(_d)
        except Exception:
            pass
    from core.data_ops import update_rows
    r = update_rows(table, set_data, where)
    if r.data.get("ok"):
        r.data["effects"] = {
            "table": table, "action": "UPDATE",
            "affected": r.data.get("affected", 0),
            "affected_ids": list(sel["ids"]),
            "changed_fields": changed_fields,
        }
        if expected_values:
            r.data["effects"]["expected_values"] = expected_values
    return r


def delete_data(table="", database="", selection_id=0):
    from core.context import get_context
    if not selection_id:
        last_id = get_context().get_last_selection_id()
        if last_id:
            selection_id = last_id
        else:
            return ToolResult.fail("请先查询数据创建选择集", code="VALIDATION",
                                   reason="need_selection")
    sel = get_context().get_selection(selection_id)
    if not sel:
        return ToolResult.fail("选择集不存在", code="NOT_FOUND",
                               reason="selection_not_found")
    table = table or sel["table"]
    from core.contract.security_contract import ids_in_clause
    where = ids_in_clause(sel['ids'])
    from core.data_ops import delete_rows
    r = delete_rows(table, where)
    if r.data.get("ok"):
        r.data["effects"] = {
            "table": table, "action": "DELETE",
            "affected": r.data.get("affected", 0),
            "affected_ids": list(sel["ids"]),
            "changed_fields": [],
        }
    return r


def mutate_data(instruction="", action="", database=""):
    """修改/删除记录的统一入口（方案C 语义，人审闸内置）：

    - 候选 0 条：如实报未找到
    - 候选 1 条：直接执行
    - 候选多条：挂起等用户确认（AI 不得直接执行批量写）
    - 候选 >100 条：要求缩小范围
    """
    if not instruction:
        return ToolResult.fail("请提供修改/删除指令（含表名、筛选条件、要改的内容）",
                               code="VALIDATION", reason="missing_params")
    from core.data_ops import mutate_natural
    return mutate_natural(instruction, action=action)
