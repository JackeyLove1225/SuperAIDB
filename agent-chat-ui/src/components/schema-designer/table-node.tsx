"use client";

import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { CheckParamValue } from "@/lib/check-templates";

export interface ColumnData {
  name: string;
  type: string;
  is_pk?: boolean;
  not_null?: boolean;
  autoincrement?: boolean;
  is_unique?: boolean;
  is_indexed?: boolean;
  check_constraint?: string;
  // CHECK 模板分类元数据（与后端 meta_columns.check_template_key/params 对齐）
  check_template_key?: string; // 模板 key（如 "int_range"），自定义时为 "custom"
  check_template_params?: Record<string, CheckParamValue>; // 参数字典，如 {min:0, max:150}
  description?: string;
}

export interface TableNodeData {
  name: string;
  business_name?: string;
  description?: string;
  datasource?: string; // 联邦数据库：表所属的数据源名
  columns: ColumnData[];
  foreign_keys?: {
    columns: string[];
    references: string;
    ref_columns: string[];
  }[];
  showColumns?: boolean; // 是否展开字段列表（由 page.tsx 的 onNodeClick 控制）
  [key: string]: unknown;
}

function TableNode({ data, selected }: NodeProps) {
  const d = data as TableNodeData;
  const allColumns = d.columns || [];
  // 外键字段集合（用于显示 🔗 图标 + 排序）
  const fkColumns = new Set<string>();
  for (const fk of d.foreign_keys || []) {
    for (const c of fk.columns || []) fkColumns.add(c);
  }

  // ── 字段排序：主键 → 外键 → 其他（保持各组内原顺序，稳定排序）──
  // 需求2：外键字段显示排序仅次于主键，让用户一眼看到字段特征
  const sortedColumns = [...allColumns].sort((a, b) => {
    const aPk = a.is_pk ? 0 : 1;
    const bPk = b.is_pk ? 0 : 1;
    if (aPk !== bPk) return aPk - bPk;
    const aFk = fkColumns.has(a.name) ? 0 : 1;
    const bFk = fkColumns.has(b.name) ? 0 : 1;
    if (aFk !== bFk) return aFk - bFk;
    return 0; // 同组保持原顺序
  });

  // 展开状态——由 page.tsx onNodeClick 控制（避免 React Flow 拖拽机制干扰内部 onClick）
  const showColumns = d.showColumns ?? false;

  // 统计约束数量（用于折叠状态摘要提示）
  const fkCount = allColumns.filter((c) => fkColumns.has(c.name)).length;
  const uniqueCount = allColumns.filter((c) => c.is_unique && !c.is_pk).length;
  const indexedCount = allColumns.filter((c) => c.is_indexed).length;
  const checkCount = allColumns.filter((c) => c.check_constraint).length;

  return (
    <div
      className={`w-56 rounded-xl border bg-white transition-colors dark:bg-zinc-900 ${
        selected
          ? "border-zinc-900 ring-2 ring-zinc-200 dark:ring-zinc-700"
          : "border-zinc-200/70 dark:border-zinc-700"
      }`}
    >
      {/* 连接点——按角色定侧：source（引用方）在左、target（被引用方）在右。
          布局上被引用表在左、引用表在右，因此外键边永远是从引用表左缘
          直连被引用表右缘的单向线，不绕弯 */}
      <Handle
        type="target"
        position={Position.Right}
        className="!h-3 !w-3 !border-2 !border-zinc-400 !bg-zinc-200"
      />
      <Handle
        type="source"
        position={Position.Left}
        className="!h-3 !w-3 !border-2 !border-zinc-400 !bg-zinc-200"
      />

      {/* 表头——点击切换展开/收起（由 page.tsx onNodeClick 统一处理） */}
      <div className="flex items-center justify-between rounded-t-[11px] bg-zinc-900 px-3 py-1.5 dark:bg-zinc-800">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <span className="shrink-0 text-[10px] text-zinc-400">
              {showColumns ? "▼" : "▶"}
            </span>
            <span className="truncate text-sm font-bold text-white">
              {d.name}
            </span>
            {d.datasource && (
              <span
                className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-700"
                title={`所属数据源: ${d.datasource}`}
              >
                {d.datasource}
              </span>
            )}
          </div>
          {d.business_name && (
            <div className="truncate pl-4 text-xs text-zinc-300">
              {d.business_name}
            </div>
          )}
        </div>
        <span className="ml-2 shrink-0 rounded bg-zinc-700 px-1.5 py-0.5 text-xs text-zinc-300">
          {allColumns.length}
        </span>
      </div>

      {/* 折叠状态：显示字段统计摘要 */}
      {!showColumns && (
        <div className="cursor-pointer px-3 py-2 text-xs text-zinc-500 dark:text-zinc-400">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className="font-medium text-zinc-700 dark:text-zinc-300"
              title="主键"
            >
              🔑 {allColumns.filter((c) => c.is_pk).length}
            </span>
            {fkCount > 0 && (
              <span
                className="text-zinc-500 dark:text-zinc-400"
                title="外键字段"
              >
                🔗 {fkCount}
              </span>
            )}
            {uniqueCount > 0 && (
              <span
                className="text-zinc-500 dark:text-zinc-400"
                title="唯一约束"
              >
                U {uniqueCount}
              </span>
            )}
            {indexedCount > 0 && (
              <span
                className="text-zinc-500 dark:text-zinc-400"
                title="索引"
              >
                I {indexedCount}
              </span>
            )}
            {checkCount > 0 && (
              <span
                className="text-zinc-500 dark:text-zinc-400"
                title="CHECK约束"
              >
                ✓ {checkCount}
              </span>
            )}
          </div>
          <div className="mt-1 text-[10px] text-zinc-400">
            点击卡片展开字段列表
          </div>
        </div>
      )}

      {/* 展开状态：显示完整字段列表（已排序：主键→外键→其他） */}
      {showColumns && (
        <div className="max-h-64 overflow-y-auto">
          {allColumns.length === 0 && (
            <div className="px-3 py-2 text-xs text-zinc-400">无字段</div>
          )}
          {sortedColumns.map((col, idx) => {
            const isPK = col.is_pk;
            const isFK = fkColumns.has(col.name);
            return (
              <div
                key={col.name}
                className={`flex items-center gap-1.5 px-3 py-1 text-xs ${
                  idx % 2 === 0
                    ? "bg-zinc-50 dark:bg-zinc-800/50"
                    : "bg-white dark:bg-zinc-900"
                }`}
              >
                <span className="w-4 shrink-0 text-center">
                  {isPK ? (
                    <span title="主键">🔑</span>
                  ) : isFK ? (
                    <span title="外键">🔗</span>
                  ) : (
                    <span className="text-zinc-300">·</span>
                  )}
                </span>
                <span
                  className={`min-w-0 flex-1 truncate font-mono ${
                    isPK
                      ? "font-bold text-zinc-900 dark:text-zinc-100"
                      : isFK
                        ? "font-semibold text-zinc-600 dark:text-zinc-300"
                        : "text-zinc-700 dark:text-zinc-300"
                  }`}
                >
                  {col.name}
                </span>
                <span className="shrink-0 text-zinc-400">{col.type}</span>
                {/* 约束标识：非空 * / 唯一 U / 索引 I / CHECK ✓ */}
                <span className="flex shrink-0 items-center gap-0.5">
                  {col.not_null && !isPK && (
                    <span
                      className="text-zinc-400"
                      title="非空"
                    >
                      *
                    </span>
                  )}
                  {col.is_unique && !isPK && (
                    <span
                      className="text-zinc-500"
                      title="唯一约束"
                    >
                      U
                    </span>
                  )}
                  {col.is_indexed && (
                    <span
                      className="text-zinc-500"
                      title="普通索引"
                    >
                      I
                    </span>
                  )}
                  {col.check_constraint && (
                    <span
                      className="text-zinc-600"
                      title={`CHECK: ${col.check_constraint}`}
                    >
                      ✓
                    </span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default memo(TableNode);
