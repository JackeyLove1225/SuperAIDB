"use client";

import { useState, useEffect, useRef } from "react";
import type { ColumnData } from "./table-node";
import type { DatasourceInfo, GraphNode, RiskReport } from "./types";
import { apiFetch, ApiError } from "@/lib/api-fetch";
import { showError } from "@/components/ui/error-modal";
import ModalOverlay from "./ModalOverlay";
import ColumnEditor from "./ColumnEditor";
import RiskConfirmDialog from "./RiskConfirmDialog";
import { useOperatorPassword } from "@/components/ui/operator-password-modal";

// ============================================================
// 表编辑器模态框——新建/编辑表结构
// ============================================================

export default function TableEditorModal({
  mode,
  initialData,
  presetDatasource,
  datasources,
  onClose,
  onSaved,
}: {
  mode: "create" | "edit";
  initialData?: GraphNode | null;
  presetDatasource?: string | null;
  datasources: DatasourceInfo[];
  onClose: () => void;
  onSaved: () => void;
}) {
  // 默认数据源：presetDatasource > initialData.datasource > 第一个默认数据源 > "primary"
  const defaultDs =
    presetDatasource ||
    initialData?.datasource ||
    datasources.find((d) => d.is_default)?.name ||
    datasources[0]?.name ||
    "primary";

  const [name, setName] = useState(initialData?.name || "");
  const [businessName, setBusinessName] = useState(
    initialData?.business_name || "",
  );
  const [description, setDescription] = useState(
    initialData?.description || "",
  );
  const [datasource, setDatasource] = useState(defaultDs);
  const [columns, setColumns] = useState<ColumnData[]>(
    initialData?.columns && initialData.columns.length > 0
      ? // 深拷贝：避免修改字段时 mutate 画布节点的原始对象（问题1根因）
        // updateColumn 的字段更新若直接修改对象引用，
        // 若不拷贝则取消编辑后画布仍显示已改类型，需手动刷新才恢复
        initialData.columns.map((c) => ({
          ...c,
          check_template_params: c.check_template_params
            ? { ...c.check_template_params }
            : undefined,
        }))
      : [
          {
            name: "id",
            type: "INTEGER",
            is_pk: true,
            not_null: true,
            autoincrement: true,
            description: "主键，自增",
          },
        ],
  );
  const [saving, setSaving] = useState(false);
  const [pendingRiskReport, setPendingRiskReport] = useState<RiskReport | null>(
    null,
  );
  // 高危操作第二因子：force=true 强制执行前弹操作密码框（普通保存不弹）
  const { askPassword, operatorPasswordModal } = useOperatorPassword();

  // 问题4修复：记录每个字段"上次成功保存的类型"，用于风险确认被拒绝时回滚 type 字段
  // - 初始化：编辑模式下记录 initialData 的类型
  // - doSave 成功后更新为本次保存的类型
  // - confirmAndForceSave 确认后更新为当前类型
  // - cancelRiskConfirm 拒绝时把 type 回滚到 committed 值（不影响其他字段修改）
  const committedTypesRef = useRef<Record<number, string>>({});
  useEffect(() => {
    if (mode === "edit" && initialData?.columns) {
      const map: Record<number, string> = {};
      initialData.columns.forEach((c, i) => {
        map[i] = c.type;
      });
      committedTypesRef.current = map;
    }
  }, [mode, initialData]);

  const addColumn = () => {
    setColumns([
      ...columns,
      {
        name: "",
        type: "TEXT",
        not_null: false,
        is_unique: false,
        is_indexed: false,
        check_constraint: "",
        description: "",
      },
    ]);
  };

  const removeColumn = (idx: number) => {
    const col = columns[idx];
    if (col.name === "id") return; // id 字段不允许删除（需求4）
    setColumns(columns.filter((_, i) => i !== idx));
  };

  const updateColumn = <K extends keyof ColumnData>(
    idx: number,
    field: K,
    value: ColumnData[K],
  ) => {
    setColumns(
      columns.map((c, i) => (i === idx ? { ...c, [field]: value } : c)),
    );
  };

  const handleSave = async () => {
    if (!name.trim()) {
      showError("请输入表名");
      return;
    }
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(name)) {
      showError("表名必须以字母或下划线开头，只含字母、数字、下划线");
      return;
    }
    // 确保有 id 主键（需求4：id 默认主键，无需复选框）
    const hasId = columns.some((c) => c.name === "id");
    if (!hasId) {
      showError("每张表必须包含 id 主键字段");
      return;
    }
    // 校验字段名
    for (const col of columns) {
      if (!col.name.trim()) {
        showError("字段名不能为空");
        return;
      }
      if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(col.name)) {
        showError(`字段名 '${col.name}' 不合法，必须以字母或下划线开头`);
        return;
      }
    }

    // CHECK 约束保存前批量硬校验（强制阻断，与后端 _normalize_and_validate_checks 二次拦截呼应）
    const allColNames = columns.map((c) => c.name);
    for (const col of columns) {
      if (!col.check_constraint) continue;
      try {
        const data = await apiFetch<{ ok: boolean; message?: string }>(
          "/api/schema-graph/validate-check",
          {
            method: "POST",
            body: JSON.stringify({
              expr: col.check_constraint,
              col_name: col.name,
              col_type: col.type,
              table_columns: allColNames,
            }),
          },
        );
        if (!data.ok) {
          showError(
            `字段 '${col.name}' 的 CHECK 约束非法:\n${data.message}\n\n表达式: ${col.check_constraint}`,
          );
          return;
        }
      } catch (e) {
        showError(
          `字段 '${col.name}' 的 CHECK 校验请求失败: ${e instanceof ApiError ? e.message : String(e)}`,
        );
        return;
      }
    }

    // 确保 id 字段始终标记为主键，其他字段不能是主键（需求4）
    const finalColumns = columns.map((c) =>
      c.name === "id"
        ? { ...c, is_pk: true, not_null: true, autoincrement: true }
        : { ...c, is_pk: false },
    );

    // 编辑模式下：先预校验表结构变更风险
    if (mode === "edit") {
      try {
        const precheckData = await apiFetch<{
          ok?: boolean;
          report?: RiskReport;
          detail?: string;
        }>(`/api/schema-graph/table/${name}/precheck`, {
          method: "POST",
          body: JSON.stringify({
            updates: {
              business_name: businessName,
              description,
              datasource,
              columns: finalColumns,
            },
          }),
        });
        if (precheckData.ok && precheckData.report?.requires_confirm) {
          // 有风险，弹出确认对话框
          setPendingRiskReport(precheckData.report);
          return;
        }
      } catch (e) {
        if (e instanceof ApiError) {
          // 预校验被后端明确拒绝（如主键保护）：HTTP 400 + detail
          showError(`预校验失败: ${e.message}`);
          return;
        }
        // precheck 失败 fail-closed：风险预校验不可用时中止保存，
        // 不让潜在高危操作绕过安全确认
        showError(
          `保存前风险预校验失败，已中止保存: ${e instanceof Error ? e.message : e}`,
        );
        return;
      }
    }

    // 正常保存（无风险 / 新建模式 / precheck 降级）
    // 写皆密码：普通保存（force:false）与新建表同样先收集操作密码
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    await doSave(finalColumns, false, operatorPassword);
  };

  /** 实际执行保存请求；写皆密码——operatorPassword 由调用方先行收集，随 body 发出 */
  const doSave = async (
    finalColumns: ColumnData[],
    force: boolean,
    operatorPassword?: string | null,
  ) => {
    setSaving(true);
    const schema = {
      name,
      business_name: businessName,
      description,
      datasource, // 联邦数据库：传递数据源名
      columns: finalColumns,
      foreign_keys: [],
      indexes: [],
    };

    try {
      const path =
        mode === "create"
          ? "/api/schema-graph/table"
          : `/api/schema-graph/table/${name}`;
      const method = mode === "create" ? "POST" : "PUT";
      const body =
        mode === "create"
          ? {
              table_schema: schema,
              x: 100,
              y: 300,
              create_real_table: true,
              operator_password: operatorPassword,
            }
          : {
              updates: {
                business_name: businessName,
                description,
                datasource,
                columns: finalColumns,
              },
              force,
              // 写皆密码：新建/普通保存/强制执行一律带操作密码，否则后端 403
              operator_password: operatorPassword,
            };

      await apiFetch(path, { method, body: JSON.stringify(body) });

      // 问题4：保存成功后，更新 committed types 为本次保存的类型（作为下次风险拒绝的回滚基准）
      finalColumns.forEach((c, i) => {
        committedTypesRef.current[i] = c.type;
      });
      onSaved();
    } catch (e) {
      // 处理 409 风险确认响应（响应体经 ApiError.data 取回）
      if (e instanceof ApiError && e.status === 409) {
        const data = (e.data ?? {}) as {
          report?: RiskReport;
          message?: string;
        };
        if (data.report) {
          setPendingRiskReport(data.report);
          return;
        }
        showError(`保存失败: ${data.message || "变更包含高危操作，需确认"}`);
        return;
      }
      showError(`保存失败: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  /** 用户确认风险后，带 force=true 重新保存（先收集操作密码；取消密码则中止） */
  const confirmAndForceSave = async () => {
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    const finalColumns = columns.map((c) =>
      c.name === "id"
        ? { ...c, is_pk: true, not_null: true, autoincrement: true }
        : { ...c, is_pk: false },
    );
    // 问题4：确认执行——把当前类型标记为"已确认"（doSave 成功后也会更新，这里提前更新防止 doSave 内部异常时基准错乱）
    columns.forEach((c, i) => {
      committedTypesRef.current[i] = c.type;
    });
    setPendingRiskReport(null);
    doSave(finalColumns, true, operatorPassword);
  };

  /** 问题4：用户取消风险确认时，回滚所有 type 字段到上次"已确认"的状态（不影响其他字段修改） */
  const cancelRiskConfirm = () => {
    const rolledBack = columns.map((c, i) => {
      const committedType = committedTypesRef.current[i];
      if (committedType !== undefined && committedType !== c.type) {
        return { ...c, type: committedType };
      }
      return c;
    });
    setColumns(rolledBack);
    setPendingRiskReport(null);
  };

  return (
    <ModalOverlay
      onClose={onClose}
      title={mode === "create" ? "➕ 新建表" : `✏️ 编辑表: ${name}`}
    >
      <div className="space-y-4">
        {/* 数据源选择——从数据库开始建表（需求2） */}
        <div className="rounded-md border border-amber-200 bg-amber-50/50 p-2 dark:border-amber-700 dark:bg-amber-950/20">
          <label className="mb-1 block text-xs font-medium text-amber-700 dark:text-amber-400">
            🗄️ 目标数据源（联邦数据库：表将创建在此数据库中）
          </label>
          <select
            value={datasource}
            onChange={(e) => setDatasource(e.target.value)}
            disabled={mode === "edit"}
            className="w-full rounded-md border border-amber-300 bg-white px-3 py-1.5 text-sm disabled:bg-amber-100/50 dark:border-amber-700 dark:bg-zinc-800 dark:disabled:bg-amber-950/30"
          >
            {datasources.length === 0 ? (
              <option value="primary">primary (默认)</option>
            ) : (
              datasources.map((ds) => (
                <option
                  key={ds.name}
                  value={ds.name}
                >
                  {ds.name} ({ds.type}){ds.is_default ? " — 默认" : ""}
                </option>
              ))
            )}
          </select>
          {mode === "edit" && (
            <p className="mt-1 text-[10px] text-zinc-400">
              编辑模式下不可更改数据源（如需迁移请删除后重建）
            </p>
          )}
        </div>

        {/* 基本信息 */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-zinc-600 dark:text-zinc-400">
              表名（英文蛇形）
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={mode === "edit"}
              className="w-full rounded-md border border-zinc-300 px-3 py-1.5 text-sm disabled:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:disabled:bg-zinc-700"
              placeholder="如 users"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-zinc-600 dark:text-zinc-400">
              业务名称（中文）
            </label>
            <input
              type="text"
              value={businessName}
              onChange={(e) => setBusinessName(e.target.value)}
              className="w-full rounded-md border border-zinc-300 px-3 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-800"
              placeholder="如 用户表"
            />
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-zinc-600 dark:text-zinc-400">
            描述
          </label>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full rounded-md border border-zinc-300 px-3 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-800"
            placeholder="表的用途说明"
          />
        </div>

        {/* 字段编辑器 */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <label className="text-xs font-medium text-zinc-600 dark:text-zinc-400">
              字段列表（id 为主键，其余字段可设置约束）
            </label>
            <button
              onClick={addColumn}
              className="rounded border border-zinc-300 px-2 py-0.5 text-xs hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              ➕ 添加字段
            </button>
          </div>
          <div className="max-h-[45vh] space-y-2 overflow-y-auto">
            {columns.map((col, idx) => {
              const isIdField = col.name === "id";
              return (
                <ColumnEditor
                  key={idx}
                  column={col}
                  isIdField={isIdField}
                  allColumnNames={columns.map((c) => c.name).filter(Boolean)}
                  onUpdate={(field, value) => updateColumn(idx, field, value)}
                  onRemove={() => removeColumn(idx)}
                />
              );
            })}
          </div>
        </div>

        {/* 操作按钮 */}
        <div className="flex justify-end gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-700">
          <button
            onClick={onClose}
            className="rounded-lg border border-zinc-200/70 bg-white px-4 py-1.5 text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            取消
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="rounded-lg bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
          >
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </div>
      {pendingRiskReport && (
        <RiskConfirmDialog
          report={pendingRiskReport}
          onCancel={cancelRiskConfirm}
          onConfirm={confirmAndForceSave}
        />
      )}
      {/* 操作密码确认弹窗（强制执行第二因子，叠在风险确认框之上） */}
      {operatorPasswordModal}
    </ModalOverlay>
  );
}
