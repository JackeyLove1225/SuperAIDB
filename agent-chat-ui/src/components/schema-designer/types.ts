// ── Schema Designer 共享类型定义 ──

import type { Node, Edge } from "@xyflow/react";
import type { TableNodeData, ColumnData } from "./table-node";
import type { DatasourceNodeData } from "./datasource-node";

// ── 后端图数据 ──

export interface GraphNode {
  id: string;
  name: string;
  business_name: string;
  description: string;
  datasource: string;
  x: number;
  y: number;
  columns: ColumnData[];
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  column: string;
  ref_column: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** true 表示图库预热中：位置/边来自 SQLite 降级快路径，就绪后应重取 */
  graph_pending?: boolean;
}

export interface Stats {
  table_count: number;
  column_count: number;
  relationship_count: number;
}

// ── 删除预检报告（后端 delete_*_precheck 产出，驱动 ImpactConfirmDialog）──

export interface DeleteTableReport {
  ok: boolean;
  table: string;
  datasource: string;
  /** null = 库中无此表或统计失败 */
  row_count: number | null;
  /** 本表引用别人（删表后随之消失） */
  outgoing_fks: { column: string; references: string; ref_column: string }[];
  /** 谁引用本表（连带影响）；rows=null 表示统计失败 */
  referenced_by: { table: string; column: string; rows: number | null }[];
}

export interface DeleteRelationshipReport {
  ok: boolean;
  table: string;
  column: string;
  references: string;
  ref_column: string;
  /** null = 统计失败 */
  affected_rows: number | null;
}

export interface DatasourceInfo {
  name: string;
  type: string;
  is_default: boolean;
  description?: string;
}

// ── React Flow 类型化节点/边 ──
// 交叉 { type: ... } 把可选的 type 收窄为字面量，使判别联合可按 type 窄化

export type TableFlowNode = Node<TableNodeData, "table"> & { type: "table" };
export type DatasourceFlowNode = Node<DatasourceNodeData, "datasource"> & { type: "datasource" };
export type FlowNode = TableFlowNode | DatasourceFlowNode;

/** 边数据：归属边（数据源→表，虚线）或外键边（表→表，实线） */
export type FlowEdgeData =
  | { kind: "ownership" }
  | { kind: "fk"; column: string; ref_column: string; cross?: boolean };
export type FlowEdge = Edge<FlowEdgeData>;

export function isDatasourceNode(node: FlowNode): node is DatasourceFlowNode {
  return node.type === "datasource";
}

// ── 风险分析报告（precheck 结果） ──

export interface RiskDataImpact {
  scanned?: number;
  fail_count?: number;
  null_count?: number;
  duplicate_groups?: number;
  fail_samples?: string[];
}

export interface RiskChange {
  type: string;
  risk: string;
  target?: string;
  description?: string;
  data_impact?: RiskDataImpact;
}

export interface RiskReport {
  risk_level?: string;
  summary?: string;
  changes?: RiskChange[];
  requires_confirm?: boolean;
}
