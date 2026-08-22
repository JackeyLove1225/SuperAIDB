"use client";

import { MGMT_API, apiFetch } from "@/lib/api-fetch";
import { signMedia, withSig } from "@/lib/media-sign";
import React, { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import RowEditorModal from "@/components/tables/RowEditorModal";

// 通过 Next.js 服务端代理访问 Management API（密钥不出服务器）


interface ColumnInfo {
  name: string;
  type: string;
  not_null: boolean;
  pk?: boolean;
}

interface TableData {
  table_name: string;
  columns: ColumnInfo[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_more: boolean;
}

export default function TableDataPage() {
  const params = useParams();
  const router = useRouter();
  const tableName = decodeURIComponent(params.tableName as string);

  const [data, setData] = useState<TableData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(50);

  // 行编辑/删除状态
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<"create" | "edit">("create");
  const [editingRow, setEditingRow] = useState<Record<string, unknown> | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Record<string, unknown> | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // 导出状态
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  // 主键列（无 pk 标记时禁用行编辑/删除）
  const pkColumn = data?.columns.find((c) => c.pk === true);

  const fetchData = useCallback(async () => {
    try {
      const json = await apiFetch<TableData>(
        `/api/database/table/${encodeURIComponent(tableName)}/data?page=${page}&page_size=${pageSize}`,
        { timeoutMs: 15000 }
      );
      setData(json);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "连接失败");
    } finally {
      setLoading(false);
    }
  }, [tableName, page, pageSize]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // 新增行
  const handleCreate = () => {
    setEditingRow(null);
    setEditorMode("create");
    setEditorOpen(true);
  };

  // 编辑行
  const handleEdit = (row: Record<string, unknown>) => {
    setEditingRow(row);
    setEditorMode("edit");
    setEditorOpen(true);
  };

  // 删除行确认
  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    if (!pkColumn) {
      setDeleteError("无法删除：未找到主键列");
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    try {
      // 参数化删除：主键列/值由后端绑定，前端不再手拼 SQL WHERE（P1-10）
      const json = await apiFetch<{ ok?: boolean; message?: string }>(
        `/api/database/table/${encodeURIComponent(tableName)}/data/delete-by-pk`,
        {
          method: "POST",
          body: JSON.stringify({
            pk_column: pkColumn.name,
            pk_value: deleteTarget[pkColumn.name],
          }),
        }
      );
      if (json && json.ok === false) throw new Error(json.message || "删除失败");
      setDeleteTarget(null);
      fetchData();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "删除失败");
    } finally {
      setDeleting(false);
    }
  };

  // 导出（后端 POST /api/export?table=<name>&format=csv|excel，再用返回的文件名触发下载）
  const handleExport = async (format: "csv" | "excel" = "csv") => {
    if (!data) return;
    setExporting(true);
    setExportError(null);
    try {
      const json = await apiFetch<{ ok?: boolean; message?: string; path?: string; detail?: string }>(
        `/api/export?table=${encodeURIComponent(data.table_name)}&format=${format}`,
        { method: "POST" }
      );
      if (json && json.ok === false) throw new Error(json.message || "导出失败");
      const filename = String(json.path || "").split(/[\\/]/).pop();
      if (!filename) throw new Error("导出成功但未返回文件名");
      // 下载是浏览器原生导航，带不了 Bearer——先签 sig 拼进 URL（kind=export 签名对象是文件名）
      const sig = await signMedia(filename, "export");
      window.location.href = withSig(
        `${MGMT_API}/api/exports/${encodeURIComponent(filename)}/download`,
        sig
      );
    } catch (e) {
      setExportError(e instanceof Error ? e.message : "导出失败");
    } finally {
      setExporting(false);
    }
  };

  if (loading && !data) {
    return (
      <div className="flex h-[calc(100vh-49px)] items-center justify-center">
        <div className="text-zinc-400">加载中...</div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="flex h-[calc(100vh-49px)] flex-col items-center justify-center gap-4">
        <div className="text-red-500">⚠️ 无法加载表数据</div>
        <div className="text-xs text-zinc-400">错误: {error}</div>
        <button
          onClick={() => router.push("/dashboard")}
          className="rounded-md bg-zinc-800 px-4 py-2 text-sm text-white hover:bg-zinc-700"
        >
          ← 返回控制台
        </button>
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className="h-[calc(100vh-49px)] overflow-y-auto bg-white dark:bg-zinc-950">
      <div className="mx-auto max-w-7xl space-y-4 p-6">
        {/* 顶部工具栏 */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={() => router.push("/dashboard")}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              ← 返回
            </button>
            <h1 className="font-mono text-xl font-bold text-zinc-900 dark:text-zinc-100">
              {data.table_name}
            </h1>
            <span className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-950 dark:text-zinc-400">
              {data.total} 行
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleCreate}
              className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700"
            >
              ➕ 新增行
            </button>
            <button
              onClick={() => handleExport("csv")}
              disabled={exporting}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              {exporting ? "导出中..." : "📥 导出 CSV"}
            </button>
            <button
              onClick={() => handleExport("excel")}
              disabled={exporting}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
            >
              {exporting ? "导出中..." : "📥 导出 Excel"}
            </button>
          </div>
        </div>

        {exportError && (
          <div className="rounded-md border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
            ⚠️ 导出失败: {exportError}
          </div>
        )}

        {/* 字段结构 */}
        <div className="rounded-[16px] border border-[#ececec] bg-white p-4 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
          <h2 className="mb-2 text-sm font-semibold text-zinc-700 dark:text-zinc-300">
            字段结构（{data.columns.length} 个字段）
          </h2>
          <div className="flex flex-wrap gap-2">
            {data.columns.map((col) => (
              <span
                key={col.name}
                className="inline-flex items-center gap-1 rounded-md bg-zinc-50 px-2 py-1 font-mono text-xs dark:bg-zinc-800"
              >
                {col.pk && <span className="text-yellow-500">🔑</span>}
                <span className="text-zinc-700 dark:text-zinc-300">{col.name}</span>
                <span className="text-zinc-400">{col.type}</span>
                {col.not_null && <span className="text-red-400">NOT NULL</span>}
              </span>
            ))}
          </div>
        </div>

        {/* 数据表格 */}
        <div className="rounded-[16px] border border-[#ececec] bg-white shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
          <div className="border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
            <span className="text-sm text-zinc-600 dark:text-zinc-400">
              第 {data.page} / {data.total_pages || 1} 页 · 每页 {data.page_size} 条
            </span>
          </div>
          <div className="overflow-x-auto">
            {data.rows.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-12">
                <div className="text-sm text-zinc-400">表中暂无数据</div>
                <button
                  onClick={handleCreate}
                  className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700"
                >
                  ➕ 新增第一行
                </button>
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-800">
                    <th className="px-3 py-2 text-left font-mono text-xs text-zinc-400">
                      #
                    </th>
                    {data.columns.map((col) => (
                      <th
                        key={col.name}
                        className="px-3 py-2 text-left font-mono text-xs font-semibold text-zinc-600 dark:text-zinc-400"
                      >
                        {col.name}
                      </th>
                    ))}
                    <th className="sticky right-0 z-10 px-3 py-2 text-left font-mono text-xs font-semibold text-zinc-600 bg-zinc-50 dark:text-zinc-400 dark:bg-zinc-800">
                      操作
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row, i) => (
                    <tr
                      key={i}
                      className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50 dark:border-zinc-900 dark:hover:bg-zinc-800"
                    >
                      <td className="px-3 py-2 font-mono text-xs text-zinc-400">
                        {(data.page - 1) * data.page_size + i + 1}
                      </td>
                      {data.columns.map((col) => {
                        const val = row[col.name];
                        return (
                          <td
                            key={col.name}
                            className="max-w-xs truncate px-3 py-2 text-zinc-700 dark:text-zinc-300"
                            title={val !== null ? String(val) : "NULL"}
                          >
                            {val === null || val === undefined ? (
                              <span className="text-zinc-400 italic">NULL</span>
                            ) : (
                              String(val)
                            )}
                          </td>
                        );
                      })}
                      <td className="sticky right-0 z-10 flex items-center gap-1 whitespace-nowrap bg-white px-3 py-2 dark:bg-zinc-900">
                        <button
                          onClick={() => handleEdit(row)}
                          disabled={!pkColumn}
                          title={pkColumn ? undefined : "无主键列，无法编辑"}
                          className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs text-zinc-700 hover:bg-zinc-100 disabled:opacity-40 disabled:hover:bg-transparent dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
                        >
                          ✏️ 编辑
                        </button>
                        <button
                          onClick={() => { setDeleteTarget(row); setDeleteError(null); }}
                          disabled={!pkColumn}
                          title={pkColumn ? undefined : "无主键列，无法删除"}
                          className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs text-red-600 hover:bg-red-50 disabled:opacity-40 disabled:hover:bg-transparent dark:border-zinc-700 dark:text-red-400 dark:hover:bg-red-950"
                        >
                          🗑 删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* 分页控件 */}
          {data.total_pages > 1 && (
            <div className="flex items-center justify-between border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={data.page <= 1}
                className="rounded-md border border-zinc-300 px-3 py-1 text-sm disabled:opacity-30 dark:border-zinc-700"
              >
                ← 上一页
              </button>
              <span className="text-sm text-zinc-500">
                {data.page} / {data.total_pages}
              </span>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={!data.has_more}
                className="rounded-md border border-zinc-300 px-3 py-1 text-sm disabled:opacity-30 dark:border-zinc-700"
              >
                下一页 →
              </button>
            </div>
          )}
        </div>
      </div>

      {/* 行编辑模态框 */}
      {data && (
        <RowEditorModal
          open={editorOpen}
          mode={editorMode}
          tableName={tableName}
          columns={data.columns}
          rowData={editingRow}
          onClose={() => setEditorOpen(false)}
          onSuccess={() => fetchData()}
        />
      )}

      {/* 删除确认对话框 */}
      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => !deleting && setDeleteTarget(null)}
        >
          <div
            className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-zinc-900"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="mb-2 text-lg font-semibold text-zinc-900 dark:text-zinc-100">
              🗑 确认删除
            </h3>
            <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
              确定要删除{" "}
              <span className="font-mono font-medium text-zinc-900 dark:text-zinc-100">
                {pkColumn ? `${pkColumn.name}=${String(deleteTarget[pkColumn.name] ?? "")}` : ""}
              </span>{" "}
              这行数据吗？此操作不可撤销。
            </p>
            {deleteError && (
              <div className="mb-4 rounded-md border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
                ⚠️ {deleteError}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
                className="rounded-md border border-zinc-300 px-4 py-2 text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
              >
                取消
              </button>
              <button
                onClick={handleDeleteConfirm}
                disabled={deleting}
                className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
              >
                {deleting ? "删除中..." : "🗑 确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
