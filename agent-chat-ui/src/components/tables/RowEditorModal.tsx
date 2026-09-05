"use client";

import React, { useState, useEffect, useMemo, useRef } from "react";
import DraggableModal from "@/components/ui/draggable-modal";
import { useOperatorPassword } from "@/components/ui/operator-password-modal";
import { apiFetch } from "@/lib/api-fetch";
import {
  getInputType,
  valueToFormStr,
  buildRowValues,
  getDirtyKeys,
  type ColumnInfo,
  type FieldValue,
} from "@/components/tables/row-editor-utils";

// ── 类型定义 ──

interface RowEditorModalProps {
  open: boolean;
  mode: "create" | "edit";
  tableName: string;
  columns: ColumnInfo[];
  rowData?: Record<string, unknown> | null;
  onClose: () => void;
  onSuccess: () => void;
}

// ── 主组件（值整形/dirty 追踪语义在 row-editor-utils.ts，有单测锁定）──

export default function RowEditorModal({
  open,
  mode,
  tableName,
  columns,
  rowData,
  onClose,
  onSuccess,
}: RowEditorModalProps) {
  const [values, setValues] = useState<Record<string, FieldValue>>({});
  const [nullFlags, setNullFlags] = useState<Record<string, boolean>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 高危操作第二因子：新增/更新行提交前弹操作密码框
  const { askPassword, operatorPasswordModal } = useOperatorPassword();
  // 编辑模式的初始值快照：提交时与之 diff，只更新"被用户改过"的字段（dirty 追踪）
  const origRef = useRef<{
    values: Record<string, FieldValue>;
    nulls: Record<string, boolean>;
  }>({
    values: {},
    nulls: {},
  });

  useEffect(() => {
    if (!open) return;
    setError(null);
    const initValues: Record<string, FieldValue> = {};
    const initNulls: Record<string, boolean> = {};
    for (const col of columns) {
      if (mode === "create") {
        initValues[col.name] = "";
        initNulls[col.name] = false;
      } else {
        const raw = rowData?.[col.name];
        const inputType = getInputType(col.type);
        if (raw === null || raw === undefined) {
          initValues[col.name] = "";
          initNulls[col.name] = !col.not_null;
        } else {
          initValues[col.name] = valueToFormStr(raw, inputType);
          initNulls[col.name] = false;
        }
      }
    }
    setValues(initValues);
    setNullFlags(initNulls);
    origRef.current = { values: initValues, nulls: initNulls };
  }, [open, mode, columns, rowData]);

  const editableColumns = useMemo(
    () => columns.filter((c) => c.pk !== true),
    [columns],
  );
  const pkColumn = useMemo(() => columns.find((c) => c.pk === true), [columns]);

  const validateForm = (dirty: Set<string>): string | null => {
    for (const col of editableColumns) {
      // 编辑模式只校验 dirty 列：未改动的列不会提交，不该因后端返回 null/空值被误伤
      if (mode === "edit" && !dirty.has(col.name)) continue;
      if (col.not_null && !nullFlags[col.name]) {
        const val = values[col.name];
        if (val === "" || val === null) {
          return `字段 "${col.name}" 不能为空（NOT NULL 约束）`;
        }
      }
      const inputType = getInputType(col.type);
      if (inputType === "number" && !nullFlags[col.name]) {
        const val = values[col.name];
        // 编辑模式：数字列被清空成空串无法落库（'' 不是合法数字），提示改用 NULL 或输入数字
        if (mode === "edit" && dirty.has(col.name) && val === "") {
          return `字段 "${col.name}" 需要数字值（${col.type}）；如需清空请勾选 NULL`;
        }
        if (val !== "" && val !== null && isNaN(Number(val))) {
          return `字段 "${col.name}" 需要数字值（${col.type}）`;
        }
      }
    }
    return null;
  };

  const handleSubmit = async () => {
    // 编辑模式先算 dirty 集（值/NULL 勾选与初始快照 diff，语义见 row-editor-utils.getDirtyKeys）：
    // 校验与提交都只针对"被用户改过"的字段
    const dirtyKeys =
      mode === "edit"
        ? getDirtyKeys(values, nullFlags, origRef.current, columns)
        : new Set<string>();
    const validationError = validateForm(dirtyKeys);
    if (validationError) {
      setError(validationError);
      return;
    }
    // 高危操作第二因子：先收集操作密码随请求发出；取消则中断提交
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "create") {
        const row = buildRowValues(values, nullFlags, columns);
        const json = await apiFetch<{ ok?: boolean; message?: string }>(
          `/api/database/table/${encodeURIComponent(tableName)}/data`,
          {
            method: "POST",
            body: JSON.stringify({
              rows: [row],
              overwrite: false,
              operator_password: operatorPassword,
            }),
          },
        );
        if (json && json.ok === false)
          throw new Error(json.message || "插入失败");
      } else {
        // 编辑 = 按主键参数化更新（update-by-pk）：只提交 dirty 字段（空串是合法值）。
        // 类型整形复用 buildRowValues，SET/WHERE 由服务端按 JSON 类型安全拼装——前端不再手拼 SQL
        const updateValues = buildRowValues(values, nullFlags, columns, {
          onlyKeys: dirtyKeys,
          keepEmpty: true,
        });
        if (Object.keys(updateValues).length === 0) {
          setError("没有修改任何字段");
          setSubmitting(false);
          return;
        }
        const pkVal = pkColumn ? rowData?.[pkColumn.name] : undefined;
        if (!pkColumn || pkVal === null || pkVal === undefined) {
          setError("无法更新：未找到主键列或主键值为空");
          setSubmitting(false);
          return;
        }
        const json = await apiFetch<{ ok?: boolean; message?: string }>(
          `/api/database/table/${encodeURIComponent(tableName)}/data/update-by-pk`,
          {
            method: "POST",
            body: JSON.stringify({
              pk_column: pkColumn.name,
              pk_value: pkVal,
              values: updateValues,
              operator_password: operatorPassword,
            }),
          },
        );
        if (json && json.ok === false)
          throw new Error(json.message || "更新失败");
      }
      onSuccess();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  // 问题5：使用 DraggableModal 支持拖拽+调整大小
  return (
    <DraggableModal
      title={
        <span>
          {mode === "create" ? "➕ 新增行" : "✏️ 编辑行"}
          <span className="ml-2 font-mono text-sm text-zinc-400">
            {tableName}
          </span>
        </span>
      }
      onClose={onClose}
      initialWidth={700}
    >
      {/* 表单内容 */}
      {error && (
        <div className="mb-4 rounded-md border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
          ⚠️ {error}
        </div>
      )}

      {mode === "edit" && pkColumn && (
        <div className="mb-4 rounded-md bg-zinc-50 px-4 py-2 dark:bg-zinc-800">
          <span className="font-mono text-xs text-zinc-400">
            主键 {pkColumn.name}（只读）
          </span>
          <div className="font-mono text-sm text-zinc-700 dark:text-zinc-300">
            {String(rowData?.[pkColumn.name] ?? "")}
          </div>
        </div>
      )}

      <div className="space-y-3">
        {editableColumns.map((col) => {
          const inputType = getInputType(col.type);
          const isNull = nullFlags[col.name] || false;
          const canBeNull = !col.not_null;
          return (
            <div
              key={col.name}
              className="flex items-start gap-3 border-b border-zinc-100 pb-3 last:border-0 dark:border-zinc-800"
            >
              <div className="w-40 shrink-0 pt-2">
                <div className="font-mono text-sm font-medium text-zinc-700 dark:text-zinc-300">
                  {col.name}
                </div>
                <div className="text-xs text-zinc-400">{col.type}</div>
                {col.not_null && (
                  <span className="text-xs text-red-400">NOT NULL</span>
                )}
              </div>
              <div className="flex-1">
                {inputType === "boolean" ? (
                  <select
                    value={isNull ? "" : String(values[col.name] ?? "0")}
                    disabled={isNull}
                    onChange={(e) =>
                      setValues((prev) => ({
                        ...prev,
                        [col.name]: e.target.value,
                      }))
                    }
                    className="w-full rounded-md border border-zinc-300 px-3 py-1.5 text-sm disabled:bg-zinc-100 disabled:text-zinc-400 dark:border-zinc-700 dark:bg-zinc-800 dark:disabled:bg-zinc-900"
                  >
                    <option value="0">false (0)</option>
                    <option value="1">true (1)</option>
                  </select>
                ) : (
                  <input
                    type={inputType}
                    value={isNull ? "" : String(values[col.name] ?? "")}
                    disabled={isNull}
                    // datetime-local 默认步长 60 秒，step=1 才允许秒级输入/显示
                    step={
                      inputType === "number"
                        ? "any"
                        : inputType === "datetime-local"
                          ? 1
                          : undefined
                    }
                    onChange={(e) =>
                      setValues((prev) => ({
                        ...prev,
                        [col.name]: e.target.value,
                      }))
                    }
                    className="w-full rounded-md border border-zinc-300 px-3 py-1.5 text-sm disabled:bg-zinc-100 disabled:text-zinc-400 dark:border-zinc-700 dark:bg-zinc-800 dark:disabled:bg-zinc-900"
                    placeholder={`输入 ${col.name}...`}
                  />
                )}
                {canBeNull && (
                  <label className="mt-1 flex items-center gap-1.5 text-xs text-zinc-400">
                    <input
                      type="checkbox"
                      checked={isNull}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        setNullFlags((prev) => ({
                          ...prev,
                          [col.name]: checked,
                        }));
                        if (!checked) {
                          // 取消 NULL 勾选：还原初始快照值，避免把未改动的原值误清成 ''
                          // （原值本身是 NULL 时保持空输入框等用户输入新值；不输入则保留原值，不进 dirty 集）
                          const orig = origRef.current;
                          if (!orig.nulls[col.name]) {
                            setValues((prev) => ({
                              ...prev,
                              [col.name]: orig.values[col.name] ?? "",
                            }));
                          }
                        }
                      }}
                      className="h-3 w-3"
                    />
                    设为 NULL
                  </label>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* 底部按钮 */}
      <div className="mt-4 flex items-center justify-end gap-2 border-t border-zinc-200 pt-4 dark:border-zinc-800">
        <button
          onClick={onClose}
          disabled={submitting}
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          取消
        </button>
        <button
          onClick={handleSubmit}
          disabled={submitting}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {submitting ? "提交中..." : mode === "create" ? "➕ 插入" : "💾 保存"}
        </button>
      </div>

      {/* 操作密码确认弹窗（高危操作第二因子，叠在行编辑器之上） */}
      {operatorPasswordModal}
    </DraggableModal>
  );
}
