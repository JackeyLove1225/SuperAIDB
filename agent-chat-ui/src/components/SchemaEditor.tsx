"use client";

import { apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect, useCallback } from "react";

// 通过 Next.js 服务端代理访问 Management API（密钥不出服务器）


// ==================== 类型定义 ====================

type ColumnType = "INTEGER" | "VARCHAR" | "FLOAT" | "TEXT" | "DATE" | "BOOLEAN";

interface Column {
  name: string;
  type: ColumnType;
  not_null?: boolean;
  description?: string;
  is_pk?: boolean;
  autoincrement?: boolean;
}

interface ForeignKey {
  columns: string[];
  references: string;
  ref_columns: string[];
}

interface Index {
  name: string;
  columns: string[];
  unique: boolean;
}

interface TableSchema {
  name: string;
  business_name: string;
  description: string;
  columns: Column[];
  foreign_keys: ForeignKey[];
  indexes: Index[];
}

interface SchemaListItem {
  name: string;
  business_name: string;
  description: string;
  column_count: number;
  fk_count: number;
  filename: string;
}

interface SchemaListResponse {
  schemas: SchemaListItem[];
  count: number;
}

interface SchemaDetailResponse {
  schema: TableSchema;
  filename: string;
}

// 后端错误响应
interface ApiErrorResponse {
  detail?: string;
  message?: string;
}

type MessageType = "success" | "error" | "warning";

interface SchemaEditorProps {
  industryName: string;
  showMessage: (msg: { type: MessageType; text: string }) => void;
}

// 受支持的字段类型
const COLUMN_TYPES: ColumnType[] = [
  "INTEGER",
  "VARCHAR",
  "FLOAT",
  "TEXT",
  "DATE",
  "BOOLEAN",
];

// 创建 id 主键列（受保护，不可编辑/删除）
function createIdColumn(): Column {
  return {
    name: "id",
    type: "INTEGER",
    not_null: true,
    is_pk: true,
    autoincrement: true,
    description: "主键",
  };
}

// 创建空表 schema（自动包含 id 主键）
function createEmptySchema(): TableSchema {
  return {
    name: "",
    business_name: "",
    description: "",
    columns: [createIdColumn()],
    foreign_keys: [],
    indexes: [],
  };
}

// ==================== 主组件 ====================

export default function SchemaEditor({
  industryName,
  showMessage,
}: SchemaEditorProps) {
  const [schemas, setSchemas] = useState<SchemaListItem[]>([]);
  const [loading, setLoading] = useState(true);
  // 编辑状态：null=列表视图，表名=编辑该表，"__new__"=新建表
  const [editingTable, setEditingTable] = useState<string | null>(null);
  const [editSchema, setEditSchema] = useState<TableSchema | null>(null);
  const [editLoading, setEditLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  // 加载表列表
  const loadSchemas = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch<SchemaListResponse>(
        `/api/industries/${industryName}/schemas`,
      );
      setSchemas(data.schemas || []);
    } catch {
      showMessage({ type: "error", text: "无法加载表列表" });
    } finally {
      setLoading(false);
    }
  }, [industryName, showMessage]);

  useEffect(() => {
    loadSchemas();
  }, [loadSchemas]);

  // 切换行业时退出编辑视图
  useEffect(() => {
    setEditingTable(null);
    setEditSchema(null);
  }, [industryName]);

  // 新建表
  const handleNew = () => {
    setEditSchema(createEmptySchema());
    setEditingTable("__new__");
  };

  // 编辑表：加载完整定义
  const handleEdit = async (tableName: string) => {
    setEditingTable(tableName);
    setEditLoading(true);
    setEditSchema(null);
    try {
      const data = await apiFetch<SchemaDetailResponse>(
        `/api/industries/${industryName}/schemas/${tableName}`,
      );
      setEditSchema(data.schema);
    } catch {
      showMessage({ type: "error", text: "加载表定义失败" });
      setEditingTable(null);
    } finally {
      setEditLoading(false);
    }
  };

  // 取消编辑
  const handleCancelEdit = () => {
    setEditingTable(null);
    setEditSchema(null);
  };

  // 保存表
  const handleSave = async () => {
    if (!editSchema) return;
    const name = editSchema.name.trim();
    if (!name) {
      showMessage({ type: "error", text: "请输入表名" });
      return;
    }
    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) {
      showMessage({
        type: "error",
        text: "表名只能包含字母、数字、下划线，且以字母开头",
      });
      return;
    }
    setSaving(true);
    try {
      await apiFetch(`/api/industries/${industryName}/schemas/${name}`, {
        method: "PUT",
        body: JSON.stringify({ schema: editSchema }),
      });
      showMessage({ type: "success", text: `表 ${name} 已保存` });
      setEditingTable(null);
      setEditSchema(null);
      await loadSchemas();
    } catch (e) {
      showMessage({ type: "error", text: e instanceof Error ? e.message : "保存失败" });
    } finally {
      setSaving(false);
    }
  };

  // 删除表（需确认）
  const handleDelete = async (tableName: string) => {
    if (!window.confirm(`确定要删除表 ${tableName} 吗？此操作不可恢复。`)) {
      return;
    }
    setDeleting(tableName);
    try {
      await apiFetch(`/api/industries/${industryName}/schemas/${tableName}`, {
        method: "DELETE",
      });
      showMessage({ type: "success", text: `表 ${tableName} 已删除` });
      await loadSchemas();
    } catch (e) {
      showMessage({ type: "error", text: e instanceof Error ? e.message : "删除失败" });
    } finally {
      setDeleting(null);
    }
  };

  // 编辑视图
  if (editingTable !== null) {
    return (
      <SchemaEditView
        schema={editSchema}
        loading={editLoading}
        saving={saving}
        isNew={editingTable === "__new__"}
        onChange={setEditSchema}
        onSave={handleSave}
        onCancel={handleCancelEdit}
      />
    );
  }

  // 列表视图
  if (loading) {
    return (
      <div className="py-8 text-center text-sm text-zinc-500">加载中...</div>
    );
  }

  return (
    <div>
      {/* 顶部操作栏 */}
      <div className="mb-4 flex items-center justify-between">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          共 <span className="font-medium">{schemas.length}</span> 个表
        </p>
        <button
          onClick={handleNew}
          className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700"
        >
          新建表
        </button>
      </div>

      {/* 表列表 */}
      {schemas.length === 0 ? (
        <p className="py-8 text-center text-sm text-zinc-500">
          暂无表结构，点击&ldquo;新建表&rdquo;开始创建
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {schemas.map((schema) => (
            <div
              key={schema.name}
              className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1">
                  <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                    {schema.name}
                  </h3>
                  {schema.business_name && (
                    <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
                      {schema.business_name}
                    </p>
                  )}
                  <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                    {schema.description || "无描述"}
                  </p>
                  <div className="mt-2 flex gap-3 text-xs text-zinc-400">
                    <span>{schema.column_count} 字段</span>
                    <span>{schema.fk_count} 外键</span>
                  </div>
                </div>
              </div>
              <div className="mt-3 flex gap-2">
                <button
                  onClick={() => handleEdit(schema.name)}
                  className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-700"
                >
                  编辑
                </button>
                <button
                  onClick={() => handleDelete(schema.name)}
                  disabled={deleting === schema.name}
                  className="rounded-md bg-red-100 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-200 disabled:opacity-50 dark:bg-red-950 dark:text-red-400 dark:hover:bg-red-900"
                >
                  {deleting === schema.name ? "删除中..." : "删除"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ==================== 编辑视图 ====================

interface SchemaEditViewProps {
  schema: TableSchema | null;
  loading: boolean;
  saving: boolean;
  isNew: boolean;
  onChange: (schema: TableSchema) => void;
  onSave: () => void;
  onCancel: () => void;
}

function SchemaEditView({
  schema,
  loading,
  saving,
  isNew,
  onChange,
  onSave,
  onCancel,
}: SchemaEditViewProps) {
  if (loading || !schema) {
    return (
      <div className="py-8 text-center text-sm text-zinc-500">加载中...</div>
    );
  }

  // 当前所有字段名（用于外键/索引选择）
  const columnNames = schema.columns.map((c) => c.name);

  // 更新基本信息字段
  const updateField = (field: keyof TableSchema, value: string) => {
    onChange({ ...schema, [field]: value });
  };

  // ==================== 字段操作 ====================

  const updateColumn = (index: number, patch: Partial<Column>) => {
    const columns = schema.columns.map((c, i) =>
      i === index ? { ...c, ...patch } : c,
    );
    onChange({ ...schema, columns });
  };

  const addColumn = () => {
    onChange({
      ...schema,
      columns: [
        ...schema.columns,
        { name: "", type: "VARCHAR", not_null: false },
      ],
    });
  };

  const removeColumn = (index: number) => {
    // 不允许删除 id 主键
    if (schema.columns[index].name === "id") return;
    const removedName = schema.columns[index].name;
    const columns = schema.columns.filter((_, i) => i !== index);
    // 同步清理引用了该字段的外键和索引
    const foreign_keys = schema.foreign_keys.filter(
      (fk) => !fk.columns.includes(removedName),
    );
    const indexes = schema.indexes
      .map((idx) => ({
        ...idx,
        columns: idx.columns.filter((c) => c !== removedName),
      }))
      .filter((idx) => idx.columns.length > 0);
    onChange({ ...schema, columns, foreign_keys, indexes });
  };

  // ==================== 外键操作 ====================

  const updateForeignKey = (index: number, patch: Partial<ForeignKey>) => {
    const foreign_keys = schema.foreign_keys.map((fk, i) =>
      i === index ? { ...fk, ...patch } : fk,
    );
    onChange({ ...schema, foreign_keys });
  };

  const addForeignKey = () => {
    onChange({
      ...schema,
      foreign_keys: [
        ...schema.foreign_keys,
        { columns: [], references: "", ref_columns: ["id"] },
      ],
    });
  };

  const removeForeignKey = (index: number) => {
    onChange({
      ...schema,
      foreign_keys: schema.foreign_keys.filter((_, i) => i !== index),
    });
  };

  // ==================== 索引操作 ====================

  const updateIndex = (index: number, patch: Partial<Index>) => {
    const indexes = schema.indexes.map((idx, i) =>
      i === index ? { ...idx, ...patch } : idx,
    );
    onChange({ ...schema, indexes });
  };

  const addIndex = () => {
    onChange({
      ...schema,
      indexes: [...schema.indexes, { name: "", columns: [], unique: false }],
    });
  };

  const removeIndex = (index: number) => {
    onChange({
      ...schema,
      indexes: schema.indexes.filter((_, i) => i !== index),
    });
  };

  return (
    <div className="space-y-5">
      {/* 基本信息 */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <h3 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {isNew ? "新建表" : `编辑表：${schema.name}`}
        </h3>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
              表名（英文标识符，字母开头）
            </label>
            <input
              type="text"
              value={schema.name}
              onChange={(e) => updateField("name", e.target.value)}
              placeholder="例如：users"
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
              业务名称
            </label>
            <input
              type="text"
              value={schema.business_name}
              onChange={(e) => updateField("business_name", e.target.value)}
              placeholder="例如：用户表"
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="mb-1 block text-xs text-zinc-600 dark:text-zinc-400">
              描述
            </label>
            <input
              type="text"
              value={schema.description}
              onChange={(e) => updateField("description", e.target.value)}
              placeholder="表的用途说明"
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
            />
          </div>
        </div>
      </section>

      {/* 字段列表 */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            字段
          </h3>
          <button
            onClick={addColumn}
            className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-700"
          >
            添加字段
          </button>
        </div>
        <div className="space-y-2">
          {schema.columns.map((col, idx) => {
            const isId = col.name === "id";
            return (
              <div
                key={idx}
                className={`flex flex-wrap items-center gap-2 rounded-md p-2 ${
                  isId
                    ? "bg-zinc-100 dark:bg-zinc-800/60"
                    : "bg-zinc-50/60 dark:bg-zinc-800/30"
                }`}
              >
                <input
                  type="text"
                  value={col.name}
                  onChange={(e) => updateColumn(idx, { name: e.target.value })}
                  disabled={isId}
                  placeholder="字段名"
                  className="w-28 min-w-0 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 disabled:opacity-60 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                />
                <select
                  value={col.type}
                  onChange={(e) =>
                    updateColumn(idx, { type: e.target.value as ColumnType })
                  }
                  disabled={isId}
                  className="w-24 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 disabled:opacity-60 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                >
                  {COLUMN_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <label className="flex shrink-0 items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
                  <input
                    type="checkbox"
                    checked={!!col.not_null}
                    onChange={(e) =>
                      updateColumn(idx, { not_null: e.target.checked })
                    }
                    disabled={isId}
                    className="h-3.5 w-3.5"
                  />
                  非空
                </label>
                {isId && (
                  <span className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                    PK
                  </span>
                )}
                <input
                  type="text"
                  value={col.description || ""}
                  onChange={(e) =>
                    updateColumn(idx, { description: e.target.value })
                  }
                  disabled={isId}
                  placeholder="描述"
                  className="min-w-0 flex-1 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 disabled:opacity-60 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                />
                {!isId && (
                  <button
                    onClick={() => removeColumn(idx)}
                    className="shrink-0 rounded bg-red-100 px-2 py-1 text-xs text-red-700 hover:bg-red-200 dark:bg-red-950 dark:text-red-400 dark:hover:bg-red-900"
                  >
                    删除
                  </button>
                )}
              </div>
            );
          })}
        </div>
        <p className="mt-2 text-xs text-zinc-400">
          id 字段为自动生成的主键，不可编辑或删除
        </p>
      </section>

      {/* 外键列表 */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            外键
          </h3>
          <button
            onClick={addForeignKey}
            className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-700"
          >
            添加外键
          </button>
        </div>
        {schema.foreign_keys.length === 0 ? (
          <p className="py-2 text-xs text-zinc-400">暂无外键</p>
        ) : (
          <div className="space-y-2">
            {schema.foreign_keys.map((fk, idx) => (
              <div
                key={idx}
                className="flex flex-wrap items-center gap-2 rounded-md bg-zinc-50/60 p-2 dark:bg-zinc-800/30"
              >
                <select
                  value={fk.columns[0] || ""}
                  onChange={(e) =>
                    updateForeignKey(idx, {
                      columns: e.target.value ? [e.target.value] : [],
                    })
                  }
                  className="w-28 min-w-0 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                >
                  <option value="">选择字段</option>
                  {columnNames.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
                <span className="shrink-0 text-xs text-zinc-400">&rarr;</span>
                <input
                  type="text"
                  value={fk.references}
                  onChange={(e) =>
                    updateForeignKey(idx, { references: e.target.value })
                  }
                  placeholder="引用表名"
                  className="w-28 min-w-0 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                />
                <span className="shrink-0 text-xs text-zinc-400">.</span>
                <input
                  type="text"
                  value={fk.ref_columns[0] || ""}
                  onChange={(e) =>
                    updateForeignKey(idx, {
                      ref_columns: e.target.value ? [e.target.value] : [],
                    })
                  }
                  placeholder="引用列"
                  className="w-20 min-w-0 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                />
                <button
                  onClick={() => removeForeignKey(idx)}
                  className="ml-auto shrink-0 rounded bg-red-100 px-2 py-1 text-xs text-red-700 hover:bg-red-200 dark:bg-red-950 dark:text-red-400 dark:hover:bg-red-900"
                >
                  删除
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 索引列表 */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            索引
          </h3>
          <button
            onClick={addIndex}
            className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-700"
          >
            添加索引
          </button>
        </div>
        {schema.indexes.length === 0 ? (
          <p className="py-2 text-xs text-zinc-400">暂无索引</p>
        ) : (
          <div className="space-y-3">
            {schema.indexes.map((idx, i) => (
              <div
                key={i}
                className="rounded-md bg-zinc-50/60 p-2 dark:bg-zinc-800/30"
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <input
                    type="text"
                    value={idx.name}
                    onChange={(e) => updateIndex(i, { name: e.target.value })}
                    placeholder="索引名"
                    className="min-w-0 flex-1 rounded border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100"
                  />
                  <label className="flex shrink-0 items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
                    <input
                      type="checkbox"
                      checked={idx.unique}
                      onChange={(e) =>
                        updateIndex(i, { unique: e.target.checked })
                      }
                      className="h-3.5 w-3.5"
                    />
                    唯一索引
                  </label>
                  <button
                    onClick={() => removeIndex(i)}
                    className="shrink-0 rounded bg-red-100 px-2 py-1 text-xs text-red-700 hover:bg-red-200 dark:bg-red-950 dark:text-red-400 dark:hover:bg-red-900"
                  >
                    删除
                  </button>
                </div>
                <div className="flex flex-wrap gap-3">
                  {columnNames.map((n) => {
                    const checked = idx.columns.includes(n);
                    return (
                      <label
                        key={n}
                        className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            const cols = e.target.checked
                              ? [...idx.columns, n]
                              : idx.columns.filter((c) => c !== n);
                            updateIndex(i, { columns: cols });
                          }}
                          className="h-3.5 w-3.5"
                        />
                        {n}
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 操作按钮 */}
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          disabled={saving}
          className="rounded-md bg-zinc-200 px-4 py-2 text-sm font-medium hover:bg-zinc-300 disabled:opacity-50 dark:bg-zinc-700 dark:text-zinc-100"
        >
          取消
        </button>
        <button
          onClick={onSave}
          disabled={saving}
          className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700 disabled:opacity-50"
        >
          {saving ? "保存中..." : "保存"}
        </button>
      </div>
    </div>
  );
}
