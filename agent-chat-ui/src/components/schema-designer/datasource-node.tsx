"use client";

import { memo } from "react";
import { Handle, Position, NodeResizer, type NodeProps } from "@xyflow/react";
import { Database, Plus } from "lucide-react";
import { TABLE_W, TABLE_H, LANE_PAD, LANE_HEADER_H } from "./layout";

export interface DatasourceNodeData {
  name: string;
  type: string;            // sqlite | mysql | postgresql
  is_default?: boolean;
  table_count?: number;    // 该数据源下的表数量
  onAddTable?: (datasource: string) => void;          // 新建表回调（useSchemaGraph 注入）
  [key: string]: unknown;
}

/**
 * 数据源泳道节点——半透明带形容器，表卡片以 parentId 归属其中
 *
 * 泳道本身就是"这张表属于哪个库"的视觉答案（替代原归属虚线）：
 * 库内连线在泳道内部，跨库连线自然穿越边界，联邦关系一眼可见。
 *
 * 交互：
 * 1. 点击右上角 ➕ → data.onAddTable 回调 → 触发新建表（数据源预填）
 * 2. 从右侧连接点拖出（到空白或其他卡片）→ 触发新建表（数据源预填）
 * 3. 双击泳道头 → 触发新建表（数据源预填）
 */
function DatasourceNode({ data, selected }: NodeProps) {
  const d = data as DatasourceNodeData;

  const typeLabels: Record<string, string> = {
    sqlite: "SQLite",
    mysql: "MySQL",
    postgresql: "PostgreSQL",
  };
  const typeLabel = typeLabels[d.type] || d.type;

  return (
    <div
      className={`h-full w-full rounded-2xl border transition-colors ${
        selected
          ? "border-zinc-400 dark:border-zinc-600"
          : "border-zinc-200/80 dark:border-zinc-800"
      } bg-zinc-50/50 dark:bg-zinc-900/30`}
    >
      {/* 手动调整泳道大小（选中时显示手柄）；拖卡片超出边界时泳道也会自适应扩大 */}
      <NodeResizer
        minWidth={TABLE_W + LANE_PAD * 2}
        minHeight={TABLE_H + LANE_PAD * 2 + LANE_HEADER_H}
        lineClassName="!border-zinc-400"
        handleClassName="!h-2.5 !w-2.5 !rounded-sm !border-zinc-400 !bg-white"
      />
      {/* 连接点——用于拖出新建表（置于泳道右缘） */}
      <Handle
        type="source"
        position={Position.Right}
        className="!h-3 !w-3 !border-2 !border-zinc-400 !bg-zinc-200"
      />
      <Handle
        type="target"
        position={Position.Left}
        className="!h-3 !w-3 !border-2 !border-zinc-400 !bg-zinc-200"
      />

      {/* 泳道头——库名/类型/表数/新建按钮 */}
      <div className="flex items-center justify-between rounded-t-2xl border-b border-zinc-200/60 bg-white/60 px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900/60">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <Database className="size-4 shrink-0 text-zinc-400" strokeWidth={1.75} />
          <span className="truncate text-sm font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            {d.name}
          </span>
          {d.is_default && (
            <span className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
              默认
            </span>
          )}
          <span className="shrink-0 text-xs text-zinc-400">
            {typeLabel} · {d.table_count ?? 0} 张表
          </span>
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation();
            d.onAddTable?.(d.name);
          }}
          className="nodrag ml-2 shrink-0 rounded-md p-1 text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-600"
          title="在此数据源新建表"
        >
          <Plus className="size-4" strokeWidth={1.75} />
        </button>
      </div>
    </div>
  );
}

export default memo(DatasourceNode);
