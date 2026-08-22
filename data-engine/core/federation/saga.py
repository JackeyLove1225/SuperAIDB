"""联邦跨库写 Saga 补偿机制

背景：联邦跨库写没有共享事务（SQLite 物理限制，各库是独立连接），
FederatedDriver.commit() 的"广播提交"不是原子的——库1提交成功、库2失败时
库1的数据就脏留了。本模块用 saga 模式兜底：

1. 写计划器把一批跨库写入拆成有序步骤（调用方按数据源分段、按依赖排序——
   主表先于明细表，见 pipeline/runner.py 的接入点）
2. 每步执行时记录补偿动作：
   - insert → 执行后回查捕获的 id，补偿时 DELETE WHERE id IN (...)
   - update/delete → 执行前 SELECT 快照，补偿时写回
3. 第 N 步失败 → 逆序补偿第 1..N-1 步
4. saga 状态逐步落盘（db/saga_journal/saga_<timestamp>_<id>.json），
   进程崩溃重启后 resume_pending() 对 failed_uncompensated 的 saga 续滚

约束（最小可用实现）：
- 不做 2PC、不做分布式锁
- 所有 SQL 走 ContractDriver 公开接口（query/execute/insert/update/delete），
  标识符校验不绕过
- 所有补偿动作写日志（可审计）

用法：
    from core.federation.saga import Saga, SagaStep
    saga = Saga()
    saga.add_step(SagaStep(datasource="primary", action="insert",
                           table="quota_item", rows=[{...}]))
    saga.add_step(SagaStep(datasource="secondary", action="insert",
                           table="material", rows=[{...}]))
    result = saga.execute()   # {ok, failed_step, compensated, error}
"""
import json
from core.logger import get_logger
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from core.contract.security_contract import (
    SecurityContract, safe_table_sql, safe_column_sql,
)

logger = get_logger(__name__)

# saga 状态机
STATUS_PENDING = "pending"                    # 已创建未执行
STATUS_COMMITTED = "committed"                # 全部步骤成功
STATUS_FAILED_UNCOMPENSATED = "failed_uncompensated"  # 失败且未完成补偿（崩溃续滚对象）
STATUS_COMPENSATED = "compensated"            # 失败后补偿完成

# 成功 saga 的 journal 保留份数（超出后清理最旧的）
JOURNAL_KEEP_COMMITTED = 20


def _default_journal_dir() -> Path:
    """默认 journal 目录：<data-engine>/db/saga_journal/"""
    return Path(__file__).resolve().parent.parent.parent / "db" / "saga_journal"


def _default_driver_resolver(datasource: str):
    """按数据源名取 ContractDriver（默认走 DataSourceManager 单例）"""
    from core.datasource_manager import DataSourceManager
    return DataSourceManager().get_driver(datasource)


def _sql_literal(v) -> str:
    """把 Python 值格式化为 SQL 字面量（用于 execute() 恢复快照）

    execute() 只接受单语句字符串、不支持参数绑定，恢复快照时需要字面量。
    字符串单引号按 '' 转义；None → NULL；bool → 1/0。
    """
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


@dataclass
class SagaStep:
    """saga 单步：一个数据源上的一批同构写入

    action=insert：rows 为待插入行；补偿数据为执行后回查的 id 列表
    action=update：需要 set_clause + where；补偿数据为执行前快照
    action=delete：需要 where；补偿数据为执行前快照
    """
    datasource: str
    action: str                       # insert / update / delete
    table: str
    rows: list = field(default_factory=list)
    where: str = ""                   # update/delete 的目标条件
    set_clause: str = ""              # update 的 SET 子句
    overwrite: bool = False           # insert 的覆盖语义
    compensation_data: dict = field(default_factory=dict)  # 执行期填充
    status: str = STATUS_PENDING      # pending / committed / compensated
    on_committed: object = None       # 可选回调 fn(saga, idx)，步骤提交后触发（依赖编排用，不落盘）


class Saga:
    """跨库写 saga：有序步骤 + 失败逆序补偿 + 状态落盘"""

    def __init__(self, journal_dir=None, saga_id: str = "",
                 driver_resolver=None, label: str = ""):
        self.saga_id = saga_id or uuid.uuid4().hex[:8]
        self.label = label
        self.created_at = time.strftime("%Y%m%d_%H%M%S")
        self.status = STATUS_PENDING
        self.steps: list[SagaStep] = []
        self.error = ""
        self._journal_dir = Path(journal_dir) if journal_dir else _default_journal_dir()
        # driver_resolver: name → ContractDriver，测试可注入，生产默认 DataSourceManager
        self._driver_resolver = driver_resolver or _default_driver_resolver

    # ── 计划 ──

    def add_step(self, step: SagaStep):
        """追加一个步骤（调用方负责排序：主表先于明细表）"""
        if step.action not in ("insert", "update", "delete"):
            raise ValueError(f"不支持的 saga 动作: {step.action}")
        # 标识符校验提前到入队时（不绕过 SecurityContract）
        SecurityContract.validate_identifier(step.datasource, "数据源名")
        SecurityContract.validate_identifier(step.table, "表名")
        self.steps.append(step)

    # ── 执行 ──

    def execute(self) -> dict:
        """逐步执行；每步成功即 committed 并落盘；失败自动逆序补偿

        Returns:
            {ok, failed_step, compensated, error, journal}
        """
        logger.info("[saga %s] 开始执行，共 %d 步 (%s)",
                    self.saga_id, len(self.steps), self.label)
        self._persist()
        for i, step in enumerate(self.steps):
            try:
                self._execute_step(step)
                step.status = STATUS_COMMITTED
                self._persist()
                logger.info("[saga %s] 步骤 %d 已提交: %s %s.%s (%d行)",
                            self.saga_id, i, step.action,
                            step.datasource, step.table, len(step.rows))
                if callable(step.on_committed):
                    step.on_committed(self, i)  # 依赖编排回调（如主表id回填明细外键）
            except Exception as e:
                self.error = str(e)[:200]
                self.status = STATUS_FAILED_UNCOMPENSATED
                # 先落盘 failed_uncompensated——此刻崩溃，resume_pending() 可续滚
                self._persist()
                logger.error("[saga %s] 步骤 %d 失败: %s %s.%s — %s",
                             self.saga_id, i, step.action,
                             step.datasource, step.table, self.error)
                try:
                    self.compensate(i)
                except Exception as ce:
                    # 补偿自身失败：保持 failed_uncompensated，等 resume_pending 续滚
                    logger.error("[saga %s] 补偿异常，待续滚: %s", self.saga_id, ce)
                    self._persist()
                    return {"ok": False, "failed_step": i, "compensated": False,
                            "error": self.error, "journal": str(self._journal_path())}
                self._persist()
                return {"ok": False, "failed_step": i, "compensated": True,
                        "error": self.error, "journal": str(self._journal_path())}

        self.status = STATUS_COMMITTED
        self._persist()
        self._cleanup_journals()
        logger.info("[saga %s] 全部 %d 步提交完成", self.saga_id, len(self.steps))
        return {"ok": True, "failed_step": -1, "compensated": False,
                "error": "", "journal": str(self._journal_path())}

    def _execute_step(self, step: SagaStep):
        """执行单步并记录补偿数据（每步独立提交——saga 无共享事务）"""
        drv = self._driver_resolver(step.datasource)
        if step.action == "insert":
            if not step.rows:
                return
            # 捕获自增 id：插入前记 max(id)，插入后回查（执行后回查）
            before = drv.query(
                f"SELECT MAX(id) AS m FROM {safe_table_sql(step.table)}")
            max_before = (before[0].get("m") or 0) if before else 0
            r = drv.insert(step.table, step.rows, step.overwrite)
            if not r.get("ok", True):
                raise RuntimeError(r.get("message", "insert 失败"))
            after = drv.query(
                f"SELECT id FROM {safe_table_sql(step.table)} WHERE id > {int(max_before)}")
            step.compensation_data = {"inserted_ids": [r["id"] for r in after]}
        elif step.action == "update":
            # 写前快照
            step.compensation_data = {"snapshot": self._snapshot(drv, step)}
            drv.update(step.table, step.set_clause, step.where)
        elif step.action == "delete":
            step.compensation_data = {"snapshot": self._snapshot(drv, step)}
            drv.delete(step.table, step.where)
        drv.commit()  # 本步落盘；后续步骤失败时靠补偿回滚本步

    def _snapshot(self, drv, step: SagaStep) -> list:
        """update/delete 执行前 SELECT 快照"""
        if not step.where:
            raise ValueError(f"saga {step.action} 步骤缺少 where 条件: {step.table}")
        SecurityContract.validate_where(step.where)
        return drv.query(
            f"SELECT * FROM {safe_table_sql(step.table)} WHERE {step.where}")

    # ── 补偿 ──

    def compensate(self, failed_step_index: int):
        """逆序补偿第 0..failed_step_index-1 步中已 committed 的步骤

        - insert 补偿：DELETE WHERE id IN (捕获的 id 列表)
        - update 补偿：按快照逐行写回原值（WHERE id=...）
        - delete 补偿：按快照逐行插回（保留原 id）
        """
        for i in range(failed_step_index - 1, -1, -1):
            step = self.steps[i]
            if step.status != STATUS_COMMITTED:
                continue
            logger.info("[saga %s] 补偿步骤 %d: %s %s.%s",
                        self.saga_id, i, step.action, step.datasource, step.table)
            self._compensate_step(step)
            step.status = "compensated"
            self._persist()
        self.status = STATUS_COMPENSATED
        logger.info("[saga %s] 补偿完成（0..%d 步已回滚）",
                    self.saga_id, failed_step_index - 1)

    def _compensate_step(self, step: SagaStep):
        drv = self._driver_resolver(step.datasource)
        data = step.compensation_data or {}
        if step.action == "insert":
            ids = data.get("inserted_ids", [])
            if ids:
                from core.contract.security_contract import ids_in_clause
                clause = ids_in_clause(ids)
                drv.delete(step.table, clause)
                logger.info("[saga %s]   DELETE %s WHERE %s",
                            self.saga_id, step.table, clause)
        elif step.action == "update":
            # 恢复快照原值；execute() 走 ContractDriver 公开接口（单语句校验）
            for row in data.get("snapshot", []):
                if "id" not in row:
                    continue
                sets = ", ".join(
                    f"{safe_column_sql(k)}={_sql_literal(v)}"
                    for k, v in row.items() if k.lower() != "id")
                if not sets:
                    continue
                drv.execute(
                    f"UPDATE {safe_table_sql(step.table)} SET {sets} "
                    f"WHERE id={int(row['id'])}")
            logger.info("[saga %s]   恢复 UPDATE 快照 %d 行: %s",
                        self.saga_id, len(data.get("snapshot", [])), step.table)
        elif step.action == "delete":
            # 插回被删除的行（保留原 id）
            for row in data.get("snapshot", []):
                cols = ", ".join(safe_column_sql(k) for k in row.keys())
                vals = ", ".join(_sql_literal(v) for v in row.values())
                drv.execute(
                    f"INSERT INTO {safe_table_sql(step.table)} ({cols}) VALUES ({vals})")
            logger.info("[saga %s]   插回 DELETE 快照 %d 行: %s",
                        self.saga_id, len(data.get("snapshot", [])), step.table)
        drv.commit()

    # ── 持久化 ──

    def _journal_path(self) -> Path:
        return self._journal_dir / f"saga_{self.created_at}_{self.saga_id}.json"

    def _persist(self):
        """状态落盘（每步状态变化后调用）"""
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        steps_data = []
        for s in self.steps:
            d = asdict(s)
            d.pop("on_committed", None)  # 回调不可序列化，崩溃续滚也不需要
            steps_data.append(d)
        payload = {
            "saga_id": self.saga_id,
            "label": self.label,
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": steps_data,
        }
        path = self._journal_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False,
                                  indent=2, default=str), encoding="utf-8")
        tmp.replace(path)  # 原子替换，避免半写文件

    def _cleanup_journals(self):
        """成功 saga 的 journal 只保留最近 JOURNAL_KEEP_COMMITTED 份"""
        try:
            files = sorted(self._journal_dir.glob("saga_*.json"))
            committed = []
            for f in files:
                try:
                    st = json.loads(f.read_text(encoding="utf-8")).get("status")
                except Exception:
                    continue
                if st == STATUS_COMMITTED:
                    committed.append(f)
            for f in committed[:-JOURNAL_KEEP_COMMITTED]:
                f.unlink(missing_ok=True)
                logger.info("[saga] 清理旧 journal: %s", f.name)
        except Exception as e:
            logger.warning("[saga] journal 清理失败（不影响主流程）: %s", e)

    # ── 崩溃续滚 ──

    @classmethod
    def _from_journal(cls, path: Path, driver_resolver=None) -> "Saga":
        data = json.loads(path.read_text(encoding="utf-8"))
        saga = cls(journal_dir=path.parent, saga_id=data["saga_id"],
                   driver_resolver=driver_resolver,
                   label=data.get("label", ""))
        saga.created_at = data.get("created_at", saga.created_at)
        saga.status = data.get("status", STATUS_PENDING)
        saga.error = data.get("error", "")
        saga.steps = [SagaStep(**s) for s in data.get("steps", [])]
        return saga

    @classmethod
    def resume_pending(cls, journal_dir=None, driver_resolver=None) -> list:
        """启动时扫描 journal 目录，对 failed_uncompensated 的 saga 续滚补偿

        Returns:
            [{saga_id, ok, error}] 每个续滚 saga 的结果
        """
        jdir = Path(journal_dir) if journal_dir else _default_journal_dir()
        results = []
        if not jdir.exists():
            return results
        for f in sorted(jdir.glob("saga_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[saga] journal 损坏跳过: %s (%s)", f.name, e)
                continue
            if data.get("status") != STATUS_FAILED_UNCOMPENSATED:
                continue
            saga = cls._from_journal(f, driver_resolver=driver_resolver)
            logger.warning("[saga %s] 发现未补偿 saga，开始续滚: %s",
                           saga.saga_id, f.name)
            try:
                # 补偿到最后一个已 committed 的步骤（含）；失败步骤本身未提交无需补偿
                last_committed = max(
                    (i for i, s in enumerate(saga.steps) if s.status == STATUS_COMMITTED),
                    default=-1)
                saga.compensate(last_committed + 1)
                saga._persist()
                results.append({"saga_id": saga.saga_id, "ok": True, "error": ""})
            except Exception as e:
                saga._persist()  # 仍是 failed_uncompensated，下次启动再试
                logger.error("[saga %s] 续滚失败: %s", saga.saga_id, e)
                results.append({"saga_id": saga.saga_id, "ok": False,
                                "error": str(e)[:200]})
        return results
