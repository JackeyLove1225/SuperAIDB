"""SchemaGraphService——统一管理 YAML ↔ SQLite 元数据 ↔ Ladybug 三层同步

三层存储架构：
  1. YAML 文件 (source of truth) — industries/{industry}/schemas/{table}.yaml
  2. SQLite 元数据表 (高性能查询) — meta_tables / meta_columns / meta_foreign_keys
  3. Ladybug 图数据库 (可视化) — 节点位置 + 外键边

三层通过 table_name 统一关联。所有写操作通过本 service 保证一致性。
读操作优先走 SQLite（微秒级），位置/关系走 Ladybug。

实际建表通过 Steward()._get_driver().create_table() 执行。
"""

import yaml
from pathlib import Path
from typing import Optional

from config.settings import settings
from core.contract.security_contract import is_valid_identifier
from core.logger import info as log_info, warning as log_warning, error as log_error
from .meta_db import MetaDB
from .ladybug_store import LadybugStore


def _get_schemas_dir() -> Path:
    """获取当前行业的 schemas 目录"""
    data_engine_root = Path(__file__).resolve().parent.parent.parent
    p = data_engine_root / "industries" / settings.INDUSTRY / "schemas"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_schema_path(table_name: str) -> Path:
    """获取表的 YAML schema 文件路径"""
    return _get_schemas_dir() / f"{table_name}.yaml"


def _kick_graph_warmup():
    """后台线程预热图库（不阻塞请求）

    幂等：LadybugStore 内部的 _lazy_starting 门闩与 bolt 预检保证
    并发/重复调用只拉起一次；成功后连接被缓存，后续请求直接命中。
    """
    import threading

    def _warm():
        try:
            LadybugStore.is_available(auto_start=True)
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()


class SchemaGraphService:
    """统一 service 层——三层存储一致性保证

    使用单例模式，复用 MetaDB 和 LadybugStore 连接。
    所有方法都是幂等的，重复调用安全。
    """

    _instance: "SchemaGraphService" = None

    @classmethod
    def get_instance(cls) -> "SchemaGraphService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（公开入口）——行业切换后必须调用，
        否则实例内缓存的旧行业 MetaDB 会继续提供旧行业数据（串台）
        """
        cls._instance = None

    def __init__(self):
        self._meta = MetaDB.get_instance()
        # 图库初始化（如果可用）
        LadybugStore.init_schema()

    # ========== 图数据读取（前端画布渲染）==========

    def get_graph(self) -> dict:
        """获取完整图——所有表节点 + 所有外键边

        性能优化：SQLite 批量查字段 + Ladybug 批量查位置/关系

        降级快路径：图库未就绪时绝不让画布请求阻塞——端口不可达则
        立即用 SQLite 数据返回（位置缺省、边取 SQLite 外键），标记
        graph_pending 并后台线程预热图库；前端收到标记后轮询重取，
        就绪后无缝获得真实布局与图库边。
        """
        # 1. 从 SQLite 获取所有表
        tables = self._meta.list_tables()
        # 2. 从 SQLite 批量获取所有字段
        all_columns = self._meta.get_all_columns()
        # 3. 图库节点位置/边：端口可达才走真实图操作；不可达走降级快路径
        graph_ready = LadybugStore._bolt_reachable()
        if graph_ready:
            graph_nodes = {n["name"]: n for n in LadybugStore.get_all_nodes()}
            graph_edges = LadybugStore.get_all_edges()
        else:
            graph_nodes, graph_edges = {}, []
            _kick_graph_warmup()

        # 组装节点
        nodes = []
        for t in tables:
            name = t["name"]
            graph_node = graph_nodes.get(name, {})
            nodes.append({
                "id": name,
                "name": name,
                "business_name": t.get("business_name", ""),
                "description": t.get("description", ""),
                "datasource": t.get("datasource", "primary"),
                "x": graph_node.get("x", 0),
                "y": graph_node.get("y", 0),
                "columns": all_columns.get(name, []),
            })

        # 组装边：优先用图库的边，降级时用 SQLite 的外键
        if graph_edges:
            edges = [{
                "id": f"{e['source']}-{e['column']}-{e['target']}",
                "source": e["source"],
                "target": e["target"],
                "column": e["column"],
                "ref_column": e["ref_column"],
            } for e in graph_edges]
        else:
            # 降级：从 SQLite 获取外键关系
            all_fks = self._meta.get_all_foreign_keys()
            edges = [{
                "id": f"{e['table_name']}-{e['column']}-{e['ref_table']}",
                "source": e["table_name"],
                "target": e["ref_table"],
                "column": e["column"],
                "ref_column": e["ref_column"],
            } for e in all_fks]

        result = {"nodes": nodes, "edges": edges}
        if not graph_ready:
            result["graph_pending"] = True
        return result

    def get_table(self, table_name: str) -> Optional[dict]:
        """获取单张表的完整信息（结构 + 字段 + 关系 + 位置）"""
        table_meta = self._meta.get_table(table_name)
        if table_meta is None:
            return None
        columns = self._meta.get_columns(table_name)
        fks = self._meta.get_foreign_keys(table_name)
        # 从图库获取位置
        graph_nodes = {n["name"]: n for n in LadybugStore.get_all_nodes()}
        graph_node = graph_nodes.get(table_name, {})
        return {
            "name": table_name,
            "business_name": table_meta.get("business_name", ""),
            "description": table_meta.get("description", ""),
            "datasource": table_meta.get("datasource", "primary"),
            "columns": columns,
            "foreign_keys": fks,
            "x": graph_node.get("x", 0),
            "y": graph_node.get("y", 0),
        }

    # ========== 表 CRUD（三层同步）==========

    def create_table(self, table_schema: dict, x: float = 0, y: float = 0,
                     create_real_table: bool = True) -> dict:
        """创建表——同步四层（YAML + SQLite + Ladybug + 实际建表）

        参数:
            table_schema: 表结构配置（name, business_name, columns, foreign_keys, ...）
            x, y: 画布初始坐标
            create_real_table: 是否实际创建数据库表（AI 预览时不创建）

        返回: {"ok": bool, "message": str}
        """
        name = table_schema.get("name", "")
        if not is_valid_identifier(name):
            return {"ok": False, "message": f"非法表名: {name}"}

        # 预校验 CHECK 约束（写 YAML 前拦截非法表达式，避免污染三层存储）
        check_err = self._normalize_and_validate_checks(table_schema.get("columns", []))
        if check_err:
            return {"ok": False, "message": check_err}

        # 快照写前三层旧状态（真实建表失败时按相反顺序回滚 Ladybug→SQLite→YAML）
        prev_yaml = self._read_yaml(name)  # None 表示本次是新建表

        try:
            # 1. 写 YAML 文件（source of truth）
            self._write_yaml(name, table_schema)

            # 2/3. SQLite 元数据 + Ladybug 节点与外键边
            self._sync_table_everywhere(
                name, table_schema,
                node_extras={
                    "datasource": table_schema.get("datasource", "primary"),
                    "x": x, "y": y,
                },
            )

            # 4. 实际建表（联邦数据库：根据 datasource 选择驱动）
            if create_real_table:
                self._create_real_table(table_schema)

            # 5. 注册到 DataSourceManager 的表映射（让联邦查询能找到该表）
            try:
                from core.datasource_manager import DataSourceManager
                dsm = DataSourceManager()
                dsm.load_config()
                dsm.register_table(name, table_schema.get("datasource"))
            except Exception as e:
                log_warning("注册表到数据源映射失败", table=name, error=str(e)[:60])

            log_info("表创建成功（三层同步）", table=name, datasource=table_schema.get("datasource", "primary"))
            return {"ok": True, "message": f"表 '{name}' 已创建"}
        except Exception as e:
            log_error("表创建失败", table=name, error=str(e)[:100])
            # 真实建表（或任一层写入）失败：按相反顺序回滚 Ladybug→SQLite meta→YAML，
            # 避免三层已落盘而真实表不存在的漂移状态
            self._rollback_create_table(name, prev_yaml)
            return {"ok": False, "message": f"建表失败，已回滚元数据: {str(e)[:180]}"}

    def update_table(self, table_name: str, updates: dict, force: bool = False) -> dict:
        """更新表结构——契约校验 + 同步三层 + 实际变更表结构

        参数:
            table_name: 表名
            updates: 更新内容（business_name, description, columns, foreign_keys）
            force: 是否强制执行高危变更（如 TEXT→INT 类型变更、加严 NOT NULL）
                   默认 False，高危变更返回 need_confirm=True 供前端二次确认

        返回:
            {"ok": bool, "message": str}
            高危变更且 force=False 时返回：
            {"ok": False, "need_confirm": True, "report": {...}, "message": str}
        """
        if not is_valid_identifier(table_name):
            return {"ok": False, "message": f"非法表名: {table_name}"}

        # 读取现有 YAML（旧 schema）
        old_schema = self._read_yaml(table_name)
        if old_schema is None:
            return {"ok": False, "message": f"表 '{table_name}' 不存在"}

        # 合并出 new_schema（深拷贝避免污染 old_schema）
        import copy
        new_schema = copy.deepcopy(old_schema)
        if "business_name" in updates:
            new_schema["business_name"] = updates["business_name"]
        if "description" in updates:
            new_schema["description"] = updates["description"]
        if "datasource" in updates:
            new_schema["datasource"] = updates["datasource"]
        if "columns" in updates:
            new_schema["columns"] = updates["columns"]
        if "foreign_keys" in updates:
            new_schema["foreign_keys"] = updates["foreign_keys"]
        new_schema["name"] = table_name

        # 预校验 CHECK 约束（合并后、写 YAML 前拦截）
        check_err = self._normalize_and_validate_checks(new_schema.get("columns", []))
        if check_err:
            return {"ok": False, "message": check_err}

        # 仅当 columns 或 foreign_keys 变更时才做契约校验和实际表结构变更
        has_schema_change = ("columns" in updates) or ("foreign_keys" in updates)

        if has_schema_change:
            # 契约校验：差异分析 + 风险评估 + 主键保护
            try:
                from core.steward import Steward
                from core.contract import SchemaChangeContract
                from core.exceptions import RiskError, PrimaryKeyError

                datasource = new_schema.get("datasource") or old_schema.get("datasource")
                drv = Steward()._get_driver(datasource)
                report = SchemaChangeContract.assert_can_update(
                    old_schema, new_schema, force, drv
                )
                # 契约校验通过后，实际变更表结构
                if "columns" in updates:
                    self._apply_column_changes(drv, table_name, report, new_schema)
            except RiskError as e:
                log_warning("表更新被契约层拦截（需确认）", table=table_name,
                            risk_level=e.report.get("risk_level", "?") if isinstance(e.report, dict) else "?")
                return {
                    "ok": False,
                    "need_confirm": True,
                    "report": e.report if isinstance(e.report, dict) else {},
                    "message": str(e),
                }
            except PrimaryKeyError as e:
                log_warning("表更新被主键保护拦截", table=table_name, error=str(e)[:80])
                return {"ok": False, "message": str(e)}
            except Exception as e:
                # 其他异常（如 SecurityError 标识符非法）
                from core.exceptions import AppError
                if isinstance(e, AppError):
                    log_warning("表更新被契约层拦截", table=table_name, error=str(e)[:80])
                    return {"ok": False, "message": str(e)}
                log_error("表更新契约校验异常", table=table_name, error=str(e)[:100])
                return {"ok": False, "message": str(e)[:200]}

        # 元数据同步（YAML + SQLite + Ladybug）
        try:
            # 1. 写 YAML
            self._write_yaml(table_name, new_schema)

            # 2/3. SQLite 元数据 + Ladybug 节点与外键边
            # columns/foreign_keys 仅在 updates 显式携带时替换对应层；
            # 外键变更走重建边模式（先删本表出向边再逐条重建，保留节点与坐标）
            self._sync_table_everywhere(
                table_name, new_schema,
                sync_columns="columns" in updates,
                sync_fks="foreign_keys" in updates,
                rebuild_graph_edges=True,
            )

            log_info("表更新成功（三层同步）", table=table_name,
                     forced=force, schema_changed=has_schema_change)
            return {"ok": True, "message": f"表 '{table_name}' 已更新"}
        except Exception as e:
            log_error("表更新失败（元数据同步阶段）", table=table_name, error=str(e)[:100])
            return {"ok": False, "message": str(e)[:200]}

    def _apply_column_changes(self, drv, table_name: str, report, new_schema: Optional[dict] = None) -> None:
        """根据变更报告实际修改数据库表结构

        遍历 ChangeReport.changes，按变更类型调用对应的 driver 方法：
        - add_column → drv.add_column
        - drop_column → drv.drop_column
        - modify_type → drv.modify_column
        - add_not_null / add_unique / modify_precision → drv.modify_column 或 alter_precision
        - drop_fk / add_fk → drv.drop_foreign_key / add_foreign_key

        主键保护已由 SchemaChangeContract 拦截，此处不再检查。
        所有变更都是契约层已校验通过的（force=True 或 safe 级别）。

        失败纪律（fail-fast）：任一列变更失败即抛 AppError 中止——
        update_table 据此返回 ok=False + 失败明细（列名+原因），且不再写
        YAML/MetaDB/图库三层元数据。与 create_table 的回滚纪律对齐取舍：
        建表路径是新对象，三层整体回滚干净；更新路径的逐条 DDL 无法简单补偿
        （已成功的列变更回滚需逆向 DDL 且可能本身失败），故底线是诚实失败——
        元数据停在旧 schema（与真实库中未变更的部分一致），已应用的列变更
        由失败信息如实告知，不假装全部成功。

        Args:
            drv: ContractDriver 实例
            table_name: 表名
            report: ChangeReport
            new_schema: 合并后的新 schema（用于 add_column 取新字段类型）

        Raises:
            AppError: 任一列变更失败（消息含变更类型+列名+原因）
        """
        from core.contract import Change

        def _resolve_new_column_type(col_name: str, data_impact: Optional[dict]) -> str:
            """add_column 类型解析：data_impact → new_schema 新定义 → MetaDB 旧定义 → TEXT(记 warning)"""
            if data_impact and data_impact.get("type"):
                return data_impact["type"]
            # new_schema（本次提交的完整定义）中查找
            for col in (new_schema or {}).get("columns", []):
                if col.get("name") == col_name and col.get("type"):
                    return col["type"]
            # MetaDB 旧定义兜底（此时尚未 replace_columns，存的还是旧结构）
            for col in self._meta.get_columns(table_name):
                if col.get("name") == col_name and col.get("type"):
                    return col["type"]
            log_warning("新增字段拿不到类型，默认 TEXT", table=table_name, column=col_name)
            return "TEXT"

        for change in report.changes:
            try:
                if change.type == "add_column":
                    # 新增字段：按 data_impact → new_schema → MetaDB 旧定义 的顺序取类型
                    col_type = _resolve_new_column_type(change.target, change.data_impact)
                    drv.add_column(table_name, change.target, col_type)
                elif change.type == "drop_column":
                    drv.drop_column(table_name, change.target)
                elif change.type == "modify_type":
                    # 类型变更：data_impact 中应包含 new_type
                    new_type = change.data_impact.get("new_type", "TEXT") if change.data_impact else "TEXT"
                    # 从 description 解析新类型（"字段类型变更: age TEXT → INTEGER"）
                    if not new_type or new_type == "TEXT":
                        parts = change.description.split("→")
                        if len(parts) >= 2:
                            new_type = parts[-1].strip()
                    drv.modify_column(table_name, change.target, new_type, force=True)
                elif change.type == "rename_table":
                    # 表重命名由上层单独处理（需更新 YAML 文件名等）
                    pass
                elif change.type == "drop_fk":
                    # 外键删除由上层 delete_relationship 处理
                    pass
                elif change.type == "add_fk":
                    # 外键新增由上层 create_relationship 处理
                    pass
                # modify_pk 已被 PrimaryKeyError 拦截，不会到这里
            except Exception as e:
                # fail-fast：不再 log 后继续写元数据（会造成元数据与真实库静默漂移）。
                # 抛 AppError 由 update_table 的契约异常分支接住 → ok=False 返回明细
                log_error("实际表结构变更失败，中止且不同步元数据", table=table_name,
                          change_type=change.type, target=change.target,
                          error=str(e)[:80])
                from core.exceptions import AppError
                raise AppError(
                    f"表结构变更失败（{change.type} {change.target}）: {str(e)[:120]}；"
                    f"元数据未改动，此前已成功的列变更请检查实际库") from e

    def precheck_update(self, table_name: str, updates: dict) -> dict:
        """预校验表结构变更（不执行）——供 server.py /precheck 路由调用

        Args:
            table_name: 表名
            updates: 更新内容

        Returns:
            {
                "ok": bool,
                "report": ChangeReport.to_dict(),  # 变更报告
                "message": str
            }
        """
        if not is_valid_identifier(table_name):
            return {"ok": False, "message": f"非法表名: {table_name}"}

        old_schema = self._read_yaml(table_name)
        if old_schema is None:
            return {"ok": False, "message": f"表 '{table_name}' 不存在"}

        import copy
        new_schema = copy.deepcopy(old_schema)
        if "business_name" in updates:
            new_schema["business_name"] = updates["business_name"]
        if "description" in updates:
            new_schema["description"] = updates["description"]
        if "datasource" in updates:
            new_schema["datasource"] = updates["datasource"]
        if "columns" in updates:
            new_schema["columns"] = updates["columns"]
        if "foreign_keys" in updates:
            new_schema["foreign_keys"] = updates["foreign_keys"]
        new_schema["name"] = table_name

        try:
            from core.steward import Steward
            from core.contract import SchemaChangeContract
            from core.exceptions import PrimaryKeyError

            datasource = new_schema.get("datasource") or old_schema.get("datasource")
            drv = Steward()._get_driver(datasource)
            report = SchemaChangeContract.precheck_update(old_schema, new_schema, drv)
            return {
                "ok": True,
                "report": report.to_dict(),
                "message": report.summary or "无变更",
            }
        except PrimaryKeyError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            from core.exceptions import AppError
            if isinstance(e, AppError):
                return {"ok": False, "message": str(e)}
            log_error("预校验异常", table=table_name, error=str(e)[:100])
            return {"ok": False, "message": str(e)[:200]}

    def delete_table_precheck(self, table_name: str) -> dict:
        """删表影响面预检（前端确认弹窗的数据源）：行数 + 正向外键 + 反向引用行数

        与聊天侧核武卡同一信息结构（方案E 对齐）：条数/正向FK/反向引用计数。
        只读统计，不修改任何存储。
        """
        meta = self._meta.get_table(table_name)
        if meta is None:
            return {"ok": False, "message": f"表 {table_name} 不存在"}
        from core.contract.security_contract import safe_table_sql, safe_column_sql
        from core.steward import Steward

        report = {
            "ok": True, "table": table_name,
            "datasource": meta.get("datasource", "primary"),
            "row_count": None,      # None = 库中无此表或统计失败
            "outgoing_fks": [],     # 本表引用别人（删表后随之消失）
            "referenced_by": [],    # 谁引用本表（连带影响；真实库删除会被契约硬阻断）
        }
        try:
            drv = Steward()._get_driver(meta.get("datasource") or None)
            if drv.table_exists(table_name):
                rows = drv.query(
                    f"SELECT COUNT(*) AS c FROM {safe_table_sql(table_name)}")
                report["row_count"] = rows[0]["c"] if rows else 0
        except Exception as e:
            log_warning("删表预检：行数统计失败", table=table_name, error=str(e)[:80])

        all_fks = self._meta.get_all_foreign_keys()
        report["outgoing_fks"] = [
            {"column": fk["column"], "references": fk["ref_table"],
             "ref_column": fk["ref_column"]}
            for fk in all_fks if fk["table_name"] == table_name
        ]
        for fk in all_fks:
            if fk["ref_table"] != table_name or fk["table_name"] == table_name:
                continue
            entry = {"table": fk["table_name"], "column": fk["column"],
                     "rows": None}
            try:
                src_meta = self._meta.get_table(fk["table_name"]) or {}
                src_drv = Steward()._get_driver(src_meta.get("datasource") or None)
                if src_drv.table_exists(fk["table_name"]):
                    rows = src_drv.query(
                        f"SELECT COUNT(*) AS c FROM {safe_table_sql(fk['table_name'])} "
                        f"WHERE {safe_column_sql(fk['column'])} IS NOT NULL")
                    entry["rows"] = rows[0]["c"] if rows else 0
            except Exception as e:
                log_warning("删表预检：反向引用统计失败",
                            table=fk["table_name"], error=str(e)[:80])
            report["referenced_by"].append(entry)
        return report

    def delete_relationship_precheck(self, table_name: str, column_name: str) -> dict:
        """删外键影响面预检：该列现有多少行数据将失去引用约束保护

        本操作只解除三层元数据的引用关系（真实库约束不在本链路维护），数据不丢；
        但解除后目标表的删改不再触发引用护栏——确认弹窗需如实告知。
        """
        fk = next((f for f in self._meta.get_foreign_keys(table_name)
                   if f["column"] == column_name), None)
        if not fk:
            return {"ok": False, "message": f"外键关系不存在: {table_name}.{column_name}"}
        meta = self._meta.get_table(table_name) or {}
        report = {"ok": True, "table": table_name, "column": column_name,
                  "references": fk["references"], "ref_column": fk["ref_column"],
                  "affected_rows": None}
        try:
            from core.contract.security_contract import safe_table_sql, safe_column_sql
            from core.steward import Steward
            drv = Steward()._get_driver(meta.get("datasource") or None)
            if drv.table_exists(table_name):
                rows = drv.query(
                    f"SELECT COUNT(*) AS c FROM {safe_table_sql(table_name)} "
                    f"WHERE {safe_column_sql(column_name)} IS NOT NULL")
                report["affected_rows"] = rows[0]["c"] if rows else 0
        except Exception as e:
            log_warning("删外键预检：行数统计失败",
                        table=table_name, column=column_name, error=str(e)[:80])
        return report

    def delete_table(self, table_name: str, drop_real_table: bool = True) -> dict:
        """删除表——同步四层"""
        if not is_valid_identifier(table_name):
            return {"ok": False, "message": f"非法表名: {table_name}"}

        # 先读取元数据中的 datasource（删除元数据后无法再获取）
        datasource = None
        table_meta = self._meta.get_table(table_name)
        if table_meta:
            datasource = table_meta.get("datasource")

        try:
            # 1. 删除 YAML 文件
            self._delete_yaml(table_name)

            # 2. SQLite 元数据（CASCADE 自动删除关联记录）
            self._meta.delete_table(table_name)

            # 3. Ladybug 节点 + 边
            LadybugStore.delete_table_node(table_name)

            # 4. 实际删表（联邦数据库：根据原 datasource 选择驱动）
            if drop_real_table:
                self._drop_real_table(table_name, datasource)

            log_info("表删除成功（三层同步）", table=table_name)
            return {"ok": True, "message": f"表 '{table_name}' 已删除"}
        except Exception as e:
            log_error("表删除失败", table=table_name, error=str(e)[:100])
            return {"ok": False, "message": str(e)[:200]}

    # ========== 位置更新（仅图库）==========

    def update_position(self, table_name: str, x: float, y: float) -> dict:
        """更新表节点的画布坐标——仅图库（高频操作，不触发其他层同步）"""
        if not is_valid_identifier(table_name):
            return {"ok": False, "message": f"非法表名: {table_name}"}
        LadybugStore.update_position(table_name, x, y)
        return {"ok": True, "message": f"位置已更新"}

    # ========== 外键关系管理 ==========

    def create_relationship(self, table_name: str, column_name: str,
                            ref_table_name: str, ref_column_name: str = "id") -> dict:
        """创建外键关系——同步三层 + 实际 ALTER TABLE

        修复BUG: 创建外键约束前先确保外键字段列存在于 DB + SQLite 元数据 + YAML。
        原实现直接调 Steward().add_foreign_key()，但 schema_manager.add_foreign_key()
        签名只接受3个参数且带 @require_consistency 装饰器会阻断操作，导致外键字段
        从未被实际添加到表中。
        """
        for n in (table_name, column_name, ref_table_name, ref_column_name):
            if not is_valid_identifier(n):
                return {"ok": False, "message": f"非法标识符: {n}"}

        try:
            from core.steward import Steward
            # 联邦数据库：根据源表 datasource 选择驱动
            src_table_meta = self._meta.get_table(table_name) or {}
            src_datasource = src_table_meta.get("datasource")
            drv = Steward()._get_driver(src_datasource)
            fk_desc = f"外键 → {ref_table_name}.{ref_column_name}"

            # 0. 修复BUG: 先确保外键字段列存在于 DB + SQLite 元数据 + YAML 三层
            # 0a. 数据库层：表存在但字段不存在则 ALTER TABLE ADD COLUMN
            try:
                if drv.table_exists(table_name) and not drv.column_exists(table_name, column_name):
                    drv.add_column(table_name, column_name, "INTEGER")
                    log_info("已添加外键字段列到数据库", table=table_name, column=column_name)
            except Exception as e:
                log_warning("DB添加外键字段列失败", error=str(e)[:80])

            # 0b. SQLite 元数据层：meta_columns 中没有该字段则插入（让前端可见）——
            # 经 MetaDB.add_column_if_missing 封装（不再直摸 _meta.conn 手写 SQL，
            # 评审四轮：service 自己也要守自己层的封装）
            self._meta.add_column_if_missing(table_name, column_name, "INTEGER", fk_desc)
            log_info("已添加外键字段列到SQLite元数据", table=table_name, column=column_name)

            # 0c. YAML 层：columns 列表中没有该字段则追加
            schema = self._read_yaml(table_name) or {}
            yaml_cols = schema.get("columns", [])
            if not any(c.get("name") == column_name for c in yaml_cols):
                yaml_cols.append({
                    "name": column_name,
                    "type": "INTEGER",
                    "description": fk_desc
                })
                schema["columns"] = yaml_cols

            # 1. SQLite 元数据 - 添加外键关系
            self._meta.add_foreign_key(table_name, column_name, ref_table_name, ref_column_name)

            # 2. Ladybug 边
            LadybugStore.create_relationship(table_name, ref_table_name, column_name, ref_column_name)

            # 3. 更新 YAML foreign_keys 列表 + 写回
            fks = schema.get("foreign_keys", [])
            new_fk = {
                "columns": [column_name],
                "references": ref_table_name,
                "ref_columns": [ref_column_name]
            }
            if new_fk not in fks:
                fks.append(new_fk)
                schema["foreign_keys"] = fks
            if schema:
                self._write_yaml(table_name, schema)

            # 4. 实际添加外键约束到数据库（直接走 Driver，避免 schema_manager 一致性检查阻断）
            # 失败时回滚三层元数据并返回显式错误——上层路由对 ok=False 会返回 400，
            # 不允许"元数据说有外键而真实库没有"的状态静默产生
            try:
                drv.add_foreign_key(table_name, column_name, ref_table_name, ref_column_name)
            except Exception as e:
                log_error("实际添加外键约束失败，回滚元数据",
                          table=table_name, column=column_name, error=str(e)[:80])
                # 回滚 SQLite 元数据外键
                try:
                    self._meta.delete_foreign_key(table_name, column_name)
                except Exception as rb:
                    log_error("回滚 SQLite 外键元数据失败", table=table_name, error=str(rb)[:80])
                # 回滚 Ladybug 边
                try:
                    LadybugStore.delete_relationship(table_name, column_name)
                except Exception as rb:
                    log_error("回滚 Ladybug 外键边失败", table=table_name, error=str(rb)[:80])
                # 回滚 YAML foreign_keys 条目
                try:
                    if schema:
                        schema["foreign_keys"] = [
                            fk for fk in schema.get("foreign_keys", [])
                            if column_name not in fk.get("columns", [])
                        ]
                        self._write_yaml(table_name, schema)
                except Exception as rb:
                    log_error("回滚 YAML 外键条目失败", table=table_name, error=str(rb)[:80])
                return {"ok": False,
                        "message": f"添加外键约束失败（元数据已回滚）: {str(e)[:150]}"}

            return {"ok": True, "message": f"外键关系已创建: {table_name}.{column_name} → {ref_table_name}.{ref_column_name}"}
        except Exception as e:
            log_error("创建外键关系失败", table=table_name, column=column_name, error=str(e)[:100])
            return {"ok": False, "message": str(e)[:200]}

    def delete_relationship(self, table_name: str, column_name: str) -> dict:
        """删除外键关系——同步三层"""
        if not is_valid_identifier(table_name) or not is_valid_identifier(column_name):
            return {"ok": False, "message": "非法标识符"}

        try:
            # 1. SQLite 元数据
            self._meta.delete_foreign_key(table_name, column_name)

            # 2. Ladybug 边
            LadybugStore.delete_relationship(table_name, column_name)

            # 3. 更新 YAML
            schema = self._read_yaml(table_name)
            if schema:
                fks = schema.get("foreign_keys", [])
                schema["foreign_keys"] = [
                    fk for fk in fks
                    if column_name not in fk.get("columns", [])
                ]
                self._write_yaml(table_name, schema)

            return {"ok": True, "message": f"外键关系已删除: {table_name}.{column_name}"}
        except Exception as e:
            return {"ok": False, "message": str(e)[:200]}

    # ========== 元数据查询 ==========

    def get_stats(self) -> dict:
        """获取统计信息"""
        return self._meta.get_stats()

    def search(self, query: str) -> dict:
        """搜索表名/字段名（利用 SQLite 索引）"""
        return self._meta.search(query)

    # ========== 启动时同步 ==========

    def sync_from_yaml(self) -> dict:
        """启动时同步：YAML → SQLite + Ladybug

        遍历当前行业的所有 schema YAML 文件，
        将缺失的表同步到 SQLite 元数据表和 Ladybug。
        """
        schemas_dir = _get_schemas_dir()
        synced = 0
        errors = 0

        for f in sorted(list(schemas_dir.glob("*.yaml")) + list(schemas_dir.glob("*.yml"))):
            table_name = f.stem
            if not is_valid_identifier(table_name):
                continue
            try:
                schema = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                # 同步到 SQLite 元数据 + Ladybug 节点与外键边
                self._sync_table_everywhere(
                    table_name, schema,
                    node_extras={"datasource": schema.get("datasource", "primary")},
                )

                synced += 1
            except Exception as e:
                log_warning("YAML 同步失败", file=f.name, error=str(e)[:60])
                errors += 1

        log_info("YAML 同步完成", synced=synced, errors=errors)
        return {"synced": synced, "errors": errors}

    def verify_reconciliation(self) -> dict:
        """三层对账：YAML ↔ MetaDB ↔ Ladybug 漂移检测（P1-8）。

        返回各方向漂移清单与图库最近写失败记录；
        ok=True 表示三层一致（图库不可用时只对 YAML↔MetaDB 两层）。
        """
        # YAML 层
        schemas_dir = _get_schemas_dir()
        yaml_tables, yaml_fks = set(), set()
        for f in sorted(list(schemas_dir.glob("*.yaml")) + list(schemas_dir.glob("*.yml"))):
            try:
                schema = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                schema = {}
            yaml_tables.add(f.stem)
            for fk in schema.get("foreign_keys", []):
                for col in fk.get("columns", []):
                    yaml_fks.add((f.stem, col, fk.get("references", "")))

        # MetaDB 层
        meta_tables = {t["name"] for t in self._meta.list_tables()}
        meta_fks = {(fk["table_name"], fk["column"], fk["ref_table"])
                    for fk in self._meta.get_all_foreign_keys()}

        # 图库层（不可用时只报不可用，不假装一致）
        graph_available = LadybugStore.is_available()
        graph_tables, graph_fks = set(), set()
        if graph_available:
            graph_tables = {n["name"] for n in LadybugStore.get_all_nodes()}
            graph_fks = {(e["source"], e["column"], e["target"])
                         for e in LadybugStore.get_all_edges()}

        # 对外契约键：graph_available/graph_warning/*_in_graph（图库层 = 嵌入式 Ladybug；
        # dashboard 消费、tests/test_08 断言锁定——前后端同仓同步改名，无外部消费者）
        drift = {
            "yaml_not_in_meta": sorted(yaml_tables - meta_tables),
            "meta_not_in_yaml": sorted(meta_tables - yaml_tables),
            "fk_yaml_not_in_meta": sorted(list(x) for x in yaml_fks - meta_fks),
            "graph_available": graph_available,
            "write_failures": LadybugStore.recent_write_failures()[-10:],
        }
        if graph_available:
            drift["yaml_not_in_graph"] = sorted(yaml_tables - graph_tables)
            drift["graph_not_in_yaml"] = sorted(graph_tables - yaml_tables)
            drift["fk_yaml_not_in_graph"] = sorted(list(x) for x in yaml_fks - graph_fks)
            drift["fk_graph_not_in_yaml"] = sorted(list(x) for x in graph_fks - yaml_fks)
        else:
            drift["graph_warning"] = "图库（Ladybug）不可用，图谱层未参与对账（图谱功能降级中）"

        layer12_ok = not drift["yaml_not_in_meta"] and not drift["meta_not_in_yaml"]
        graph_ok = (not graph_available) or (
            not drift.get("yaml_not_in_graph") and not drift.get("graph_not_in_yaml")
            and not drift.get("fk_yaml_not_in_graph") and not drift.get("fk_graph_not_in_yaml"))
        drift["ok"] = layer12_ok and graph_ok
        return drift

    # ========== 内部辅助方法 ==========

    def _sync_table_everywhere(self, table_name: str, schema: dict, *,
                               sync_columns: bool = True, sync_fks: bool = True,
                               node_extras: Optional[dict] = None,
                               rebuild_graph_edges: bool = False) -> None:
        """表结构三层同步公共序列（create_table/update_table/sync_from_yaml 收敛）：

        SQLite 元数据 upsert_table → replace_columns → replace_foreign_keys
        → Ladybug upsert_table_node → 逐 fk create_relationship。

        各调用方的差异以参数表达（均为原行为的原样保留，不引入新语义）：
        - sync_columns / sync_fks：update_table 仅在 updates 显式携带
          columns/foreign_keys 时才替换对应元数据与图库边
        - node_extras：Ladybug 节点 upsert 的透传参数——create_table 传
          datasource/x/y（x/y 仅建节点时生效），sync_from_yaml 传 datasource，
          update_table 不传（沿用 upsert_table_node 的默认参数形状）
        - rebuild_graph_edges：True 时先 delete_relationships_from 再逐条重建
          本表出向外键边（update_table 的外键变更路径；只删出向边、保留节点
          本身——整节点 DETACH DELETE 会把画布坐标重置为 (0,0) 且丢入向边）
        """
        # 1. SQLite 元数据
        self._meta.upsert_table(
            name=table_name,
            business_name=schema.get("business_name", ""),
            description=schema.get("description", ""),
            datasource=schema.get("datasource", "primary"),
        )
        if sync_columns:
            self._meta.replace_columns(table_name, schema.get("columns", []))
        if sync_fks:
            self._meta.replace_foreign_keys(table_name, schema.get("foreign_keys", []))

        # 2. Ladybug 节点
        LadybugStore.upsert_table_node(
            name=table_name,
            business_name=schema.get("business_name", ""),
            description=schema.get("description", ""),
            **(node_extras or {}),
        )
        # 3. Ladybug 外键边
        if sync_fks:
            if rebuild_graph_edges:
                LadybugStore.delete_relationships_from(table_name)
            for fk in schema.get("foreign_keys", []):
                ref_table = fk.get("references", "")
                for col in fk.get("columns", []):
                    for ref_col in fk.get("ref_columns", ["id"]):
                        LadybugStore.create_relationship(table_name, ref_table, col, ref_col)

    def _normalize_and_validate_checks(self, columns: list[dict]) -> str:
        """预校验并规范化字段的 CHECK 约束

        返回空串表示通过，非空串为中文错误信息（调用方直接返回给前端）。

        对每个字段：
        - 若 check_template_key 非 custom 且非空：用 render_expr 重新渲染 check_constraint
          （以模板+参数为准，忽略前端可能传的脏 check_constraint）
        - 若 check_constraint 非空：validate_check_expr 兜底校验
        """
        from core.graph.check_templates import render_expr
        from core.drivers.checks import validate_check_expr

        # 收集所有列名（用于跨列约束校验，如 end_date > start_date）
        all_col_names = [c.get("name", "") for c in columns if c.get("name")]

        for col in columns:
            col_name = col.get("name", "")
            tmpl_key = col.get("check_template_key", "")
            check_expr = col.get("check_constraint", "")

            # 有模板 key 且非 custom：用模板重新渲染（覆盖前端可能传的脏表达式）
            if tmpl_key and tmpl_key != "custom":
                params = col.get("check_template_params") or {}
                rendered = render_expr(tmpl_key, col_name, params)
                if rendered:
                    check_expr = rendered
                    col["check_constraint"] = rendered  # 规范化回写
                elif check_expr:
                    # 模板渲染失败但已有表达式，保留原表达式（可能是参数缺失，后续校验会拦截）
                    pass
                else:
                    return f"字段 '{col_name}' 的 CHECK 模板 '{tmpl_key}' 渲染失败（参数缺失）"

            # 兜底校验表达式
            if check_expr:
                ok, msg = validate_check_expr(
                    check_expr, col_name, col.get("type", ""), all_col_names
                )
                if not ok:
                    return f"字段 '{col_name}' 的 CHECK 约束非法: {msg}"

        return ""

    def _read_yaml(self, table_name: str) -> Optional[dict]:
        """读取 YAML schema 文件——薄委托 schema_matcher.load_table_schema（P2-5 加载收敛）"""
        from core.schema_matcher import load_table_schema
        return load_table_schema(table_name)

    def _write_yaml(self, table_name: str, schema: dict):
        """写入 YAML schema 文件"""
        schema["name"] = table_name
        f = _get_schema_path(table_name)
        f.write_text(
            yaml.dump(schema, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8"
        )

    def _delete_yaml(self, table_name: str):
        """删除 YAML schema 文件"""
        for ext in [".yaml", ".yml"]:
            f = _get_schema_path(table_name).with_suffix(ext)
            if f.exists():
                f.unlink()

    def _rollback_create_table(self, table_name: str, prev_yaml: Optional[dict]):
        """create_table 失败回滚——按相反顺序回滚 Ladybug → SQLite meta → YAML

        与 schema_manager._save_with_rollback 同思路（DB 失败后回滚已落盘的层），
        但本 service 是三层存储，需逐层反向回滚：
        - prev_yaml 为 None（新建表）：三层全部清除
        - prev_yaml 非 None（覆盖已有表失败）：YAML 恢复旧内容，SQLite 按旧 YAML 重建，
          Ladybug 节点保留（upsert 用 COALESCE 未动坐标，避免删节点丢坐标）

        每层回滚失败只记 error 日志，继续回滚其余层。
        """
        # 1. Ladybug 节点 + 边
        try:
            if prev_yaml is None:
                LadybugStore.delete_table_node(table_name)
        except Exception as e:
            log_error("回滚 Ladybug 节点失败", table=table_name, error=str(e)[:80])
        # 2. SQLite 元数据
        try:
            if prev_yaml is None:
                self._meta.delete_table(table_name)
            else:
                self._meta.upsert_table(
                    name=table_name,
                    business_name=prev_yaml.get("business_name", ""),
                    description=prev_yaml.get("description", ""),
                    datasource=prev_yaml.get("datasource", "primary"),
                )
                self._meta.replace_columns(table_name, prev_yaml.get("columns", []))
                self._meta.replace_foreign_keys(table_name, prev_yaml.get("foreign_keys", []))
        except Exception as e:
            log_error("回滚 SQLite 元数据失败", table=table_name, error=str(e)[:80])
        # 3. YAML 文件
        try:
            if prev_yaml is None:
                self._delete_yaml(table_name)
            else:
                self._write_yaml(table_name, prev_yaml)
        except Exception as e:
            log_error("回滚 YAML 文件失败", table=table_name, error=str(e)[:80])

    def _create_real_table(self, table_schema: dict):
        """实际创建数据库表——联邦数据库：根据 datasource 选择驱动

        规范化字段名：前端/元数据用 is_unique/is_indexed/check_constraint，
        驱动层用 unique/check。is_indexed 列需单独建索引（驱动 create_table 不处理）。
        """
        import copy
        from core.steward import Steward
        datasource = table_schema.get("datasource")
        drv = Steward()._get_driver(datasource)

        normalized = copy.deepcopy(table_schema)
        table_name = normalized.get("name", "")
        indexed_columns = []
        for col in normalized.get("columns", []):
            # is_unique → unique（驱动识别 unique）
            if col.get("is_unique"):
                col["unique"] = True
            # check_constraint → check（驱动识别 check）
            if col.get("check_constraint"):
                col["check"] = col["check_constraint"]
            # 收集 is_indexed 列（驱动不直接支持，需单独建索引）
            if col.get("is_indexed"):
                indexed_columns.append(col["name"])

        # 建表（驱动处理 pk/not_null/unique/check）
        drv.create_table(normalized)

        # 为 is_indexed 列创建普通索引（项目约定：索引默认唯一，但 is_indexed 是普通索引）
        for col_name in indexed_columns:
            try:
                drv.create_index(table_name, col_name, unique=False)
            except Exception as e:
                log_warning("创建索引失败", table=table_name, column=col_name, error=str(e)[:60])

    def _drop_real_table(self, table_name: str, datasource: str = None):
        """实际删除数据库表（联邦数据库：根据 datasource 选择驱动）"""
        from core.steward import Steward
        drv = Steward()._get_driver(datasource)
        if drv.table_exists(table_name):
            drv.drop_table(table_name)
