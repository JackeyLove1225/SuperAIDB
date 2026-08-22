// ── Schema Designer 自动布局（dagre 层级布局）──
//
// 布局语义：
// 1. 泳道（数据源）纵向堆叠，每个数据源一个水平带形容器
// 2. 泳道内表卡片按外键拓扑 LR 分层：被引用的主表在左，引用方在右，
//    连线方向自然形成"基础数据 → 业务数据"的阅读方向
// 3. 手写拖拽位置优先（经 /api/schema-graph 持久化；图库未就绪时走 SQLite 降级快路径，
//    就绪后无缝升级），无位置/位置越界的表回退到 dagre 落位

import dagre from "@dagrejs/dagre";
import type { GraphEdge } from "./types";

export const TABLE_W = 224; // w-56
export const TABLE_H = 96; // 折叠态高度（展开态更高，布局按折叠算即可）
export const LANE_HEADER_H = 40;
export const LANE_PAD = 20;
export const LANE_GAP = 60;

export interface TablePos {
  x: number;
  y: number;
}

export interface LaneBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 对单个数据源内的表做 dagre 层级布局
 * @param tableIds 该数据源内的表名
 * @param allEdges 全量外键边（只取两端都在本数据源内的边）
 * @returns 表名 → 相对泳道内容区的坐标（不含泳道头/内边距）
 */
export function layoutLaneTables(
  tableIds: string[],
  allEdges: GraphEdge[],
): Map<string, TablePos> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 36, ranksep: 90, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));

  const inLane = new Set(tableIds);
  for (const id of tableIds) {
    g.setNode(id, { width: TABLE_W, height: TABLE_H });
  }
  for (const e of allEdges) {
    if (!inLane.has(e.source) || !inLane.has(e.target)) continue;
    // 反转加边：被引用表（target）排在左，引用表（source）排在右
    g.setEdge(e.target, e.source);
  }
  dagre.layout(g);

  const out = new Map<string, TablePos>();
  for (const id of tableIds) {
    const n = g.node(id);
    if (n) out.set(id, { x: n.x - TABLE_W / 2, y: n.y - TABLE_H / 2 });
  }
  return out;
}

/** 泳道内表的默认相对坐标（泳道内容区原点 = 内边距 + 头部高度） */
export function laneContentOffset(): TablePos {
  return { x: LANE_PAD, y: LANE_HEADER_H + LANE_PAD };
}

/**
 * 卡片有效高度：展开字段列表后卡片显著变高，泳道尺寸计算必须按此估算，
 * 否则展开边缘卡片会溢出泳道下边框
 */
export function tableEffectiveHeight(columnCount: number, expanded: boolean): number {
  if (!expanded) return TABLE_H;
  const ROW_H = 24; // 字段行高（py-1 + text-xs）
  const HEADER_H = 56; // 表头（标题 + 业务名）
  const MAX_BODY = 256; // 字段区 max-h-64
  const FOOTER_PAD = 8;
  return HEADER_H + Math.min(columnCount * ROW_H, MAX_BODY) + FOOTER_PAD;
}
