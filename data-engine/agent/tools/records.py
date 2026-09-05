"""记录写域——记录级增/改/删工具 handler。

insert_data / batch_insert_data / edit_data / delete_data。
（自然语言改/删的统一入口在 core.data_ops.mutate_natural——树路由经
instruct.py 直连它，工具面不再重复暴露薄包装）
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


def _parse_frozen_ids(frozen_ids):
    """冻结 id 集解析：返回 (ids, None) 或 (None, ToolResult 失败)"""
    try:
        ids = _json.loads(frozen_ids) if isinstance(frozen_ids, str) else frozen_ids
    except (ValueError, TypeError):
        return None, ToolResult.fail("冻结 id 集格式错误（须为 JSON 数组）",
                                     code="VALIDATION", reason="data_format")
    if not isinstance(ids, list):
        return None, ToolResult.fail("冻结 id 集须为数组",
                                     code="VALIDATION", reason="data_format")
    return ids, None


def _resolve_frozen(frozen_ids, table):
    """冻结载荷解析（人审桥结算重放通道）：返回 (ids, table, err)。
    冻结的 id 集即批准对象——绕开选择集直执，批准对象=执行对象精确同一"""
    ids, err = _parse_frozen_ids(frozen_ids)
    if err:
        return None, None, err
    if not table:
        return None, None, ToolResult.fail("冻结结算缺少表名", code="VALIDATION",
                                           reason="missing_params")
    return ids, table, None


def _resolve_selection(selection_id, table):
    """选择集解析（最近回退 + 存在性 + 主体归属）：返回 (sel, table, err)。
    主体归属校验：选择集带 owner（channel:pid:role），不匹配即拒——
    A 客户端不能再经 selection_id 摸到 B 客户端的查询结果"""
    from core.context import get_context
    if not selection_id:
        # 合理默认值：用最近选择集（与 database 默认第一个、table 从选择集默认一致）
        last_id = get_context().get_last_selection_id()
        if last_id:
            selection_id = last_id
        else:
            return None, None, ToolResult.fail("请先查询数据创建选择集",
                                               code="VALIDATION", reason="need_selection")
    sel = get_context().get_selection(selection_id)
    if not sel:
        return None, None, ToolResult.fail("选择集不存在", code="NOT_FOUND",
                                           reason="selection_not_found")
    _owner = sel.get("owner")
    if _owner and _owner != get_context()._current_owner():
        return None, None, ToolResult.fail("选择集不属于当前会话主体，请重新查询",
                                           code="VALIDATION", reason="selection_owner_mismatch")
    return sel, table or sel["table"], None


def insert_data(table="", data="", database=""):
    if not table: return ToolResult.fail("请指定表名", code="VALIDATION", reason="missing_params")
    if not data: return ToolResult.fail("请指定数据内容", code="VALIDATION", reason="missing_params")
    try: row = _json.loads(data) if isinstance(data,str) else data
    except (ValueError, TypeError): return ToolResult.fail("data 格式错误，请使用 JSON 格式", code="VALIDATION", reason="data_format")
    if not isinstance(row, dict):
        return ToolResult.fail('data 须为 JSON 对象（{"字段名":"值"}）',
                               code="VALIDATION", reason="data_format")
    if any(k.lower()=="id" for k in row.keys()):
        return ToolResult.fail("id 是系统主键，由系统自动生成，不允许手动指定",
                               code="VALIDATION", reason="primary_key")
    from core.data_ops import insert_row
    return insert_row(table, _json.dumps(row,ensure_ascii=False))


def batch_insert_data(table="", data="", database=""):
    if not table: return ToolResult.fail("请指定表名", code="VALIDATION", reason="missing_params")
    if not data: return ToolResult.fail("请指定批量数据内容", code="VALIDATION", reason="missing_params")
    try: rows = _json.loads(data) if isinstance(data,str) else data
    except (ValueError, TypeError): return ToolResult.fail("data 格式错误，请使用 JSON 格式", code="VALIDATION", reason="data_format")
    if not isinstance(rows,list): rows = [rows]
    if not all(isinstance(r, dict) for r in rows):
        return ToolResult.fail('批量数据的每行都须是 JSON 对象（{"字段名":"值"}）',
                               code="VALIDATION", reason="data_format")
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


def _normalize_set_data(set_data):
    """set_data 归一（两分支共用：冻结结算/选择集）：dict 或 JSON 对象串 →
    SQL 赋值串；返回 (set_data_str, changed_fields, expected_values, err)。
    expected_values 仅 dict 形态保留（写操作 effects 对账用）"""
    if not set_data:
        return None, None, None, ToolResult.fail(
            "请指定要修改的内容（如 a2=888）", code="VALIDATION",
            reason="missing_params")
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
    expected_values = None
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
            pass  # dict 形态 set_data 解析失败则按字符串原样透传
    return set_data, _set_fields_of(set_data), expected_values, None


def edit_data(table="", database="", selection_id=0, set_data="", frozen_ids=""):
    # set_data 先归一（开放层 AI 常传 JSON 对象，update_rows/契约层只认
    # SQL 赋值串）——冻结结算与选择集两路径同一归一，同人审闸同命
    set_data, changed_fields, expected_values, err = _normalize_set_data(set_data)
    if err:
        return err
    # 人审桥冻结载荷（单表改/删的跨进程结算重放）：冻结的 id 集即批准对象——
    # 绕开选择集（登记进程的主体载荷，跨进程/跨主体读不到）直执，
    # 批准对象=执行对象精确同一
    if frozen_ids:
        ids, table, err = _resolve_frozen(frozen_ids, table)
        if err:
            return err
        from core.contract.security_contract import ids_in_clause
        from core.data_ops import update_rows
        r = update_rows(table, set_data, ids_in_clause(ids))
        if r.data.get("ok"):
            # effects 挂账与选择集路径同构（结算回执/对账同宽）
            r.data["effects"] = {
                "table": table, "action": "UPDATE",
                "affected": r.data.get("affected", 0),
                "affected_ids": list(ids),
                "changed_fields": changed_fields,
            }
            if expected_values:
                r.data["effects"]["expected_values"] = expected_values
        return r
    sel, table, err = _resolve_selection(selection_id, table)
    if err:
        return err
    from core.contract.security_contract import ids_in_clause
    where = ids_in_clause(sel['ids'])
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


def delete_data(table="", database="", selection_id=0, frozen_ids=""):
    # 人审桥冻结载荷（单表改/删的跨进程结算重放）：冻结的 id 集即批准对象——
    # 绕开选择集（登记进程的主体载荷，跨进程/跨主体读不到）直执，
    # 批准对象=执行对象精确同一
    if frozen_ids:
        ids, table, err = _resolve_frozen(frozen_ids, table)
        if err:
            return err
        from core.contract.security_contract import ids_in_clause
        from core.data_ops import delete_rows
        return delete_rows(table, ids_in_clause(ids))
    sel, table, err = _resolve_selection(selection_id, table)
    if err:
        return err
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
