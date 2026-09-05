"use client";

import {
  useState,
  useCallback,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";
import { type OnNodeDrag } from "@xyflow/react";

import {
  isDatasourceNode,
  type TableFlowNode,
  type FlowNode,
} from "@/components/schema-designer/types";
import {
  tableEffectiveHeight,
  TABLE_W,
  TABLE_H,
  LANE_PAD,
  LANE_HEADER_H,
} from "@/components/schema-designer/layout";
import { apiFetch } from "@/lib/api-fetch";

// ── 布局版本 / 泳道自适应 / 拖拽坐标持久化 Hook（布局面）──

export function useGraphLayout({
  nodes,
  setNodes,
}: {
  nodes: FlowNode[];
  setNodes: Dispatch<SetStateAction<FlowNode[]>>;
}) {
  // 布局版本号：图结构/位置真正变化时 +1，驱动画布 remount 并自动 fitView
  const [layoutVersion, setLayoutVersion] = useState(0);
  const bumpLayoutVersion = useCallback(
    () => setLayoutVersion((v) => v + 1),
    [],
  );

  // 拖拽位置保存防抖
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── 泳道自适应：拖动表卡片时泳道实时扩大包住，放下后收缩到内容贴合 ──

  const onNodeDrag = useCallback<OnNodeDrag<FlowNode>>(
    (_evt, node) => {
      if (isDatasourceNode(node) || !node.parentId) return;
      // 只增不减：拖动过程中实时扩大泳道（收缩留给 onNodeDragStop 统一贴合）
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id !== node.parentId || !isDatasourceNode(n)) return n;
          const curW = Number(n.style?.width) || 0;
          const curH = Number(n.style?.height) || 0;
          const needW = node.position.x + TABLE_W + LANE_PAD;
          const needH =
            node.position.y +
            tableEffectiveHeight(
              (node.data.columns || []).length,
              !!node.data.showColumns,
            ) +
            LANE_PAD;
          if (needW <= curW && needH <= curH) return n;
          return {
            ...n,
            style: {
              ...n.style,
              width: Math.max(needW, curW),
              height: Math.max(needH, curH),
            },
          };
        }),
      );
    },
    [setNodes],
  );

  /**
   * 泳道四向归一化：左/上方向平移泳道框并反向补偿全部子卡片
   * （卡片视觉位置不变，边框跟上），右/下方向按内容收尺寸。
   * 返回归一化后的节点数组与平移量（用于把被拖卡片的补偿后坐标持久化）。
   */
  const normalizeLane = useCallback(
    (
      nds: FlowNode[],
      laneId: string,
    ): { next: FlowNode[]; dx: number; dy: number } => {
      const lane = nds.find((n) => n.id === laneId);
      if (!lane || !isDatasourceNode(lane)) return { next: nds, dx: 0, dy: 0 };
      const children = nds.filter(
        (n): n is TableFlowNode =>
          !isDatasourceNode(n) && n.parentId === laneId,
      );
      if (children.length === 0) return { next: nds, dx: 0, dy: 0 };
      let minX = Infinity,
        minY = Infinity,
        maxX = -Infinity,
        maxY = -Infinity;
      for (const c of children) {
        minX = Math.min(minX, c.position.x);
        minY = Math.min(minY, c.position.y);
        maxX = Math.max(maxX, c.position.x + TABLE_W);
        // 高度按有效高度：展开的卡片按字段数估算（展开边缘卡片不溢出下边框）
        const h = tableEffectiveHeight(
          (c.data.columns || []).length,
          !!c.data.showColumns,
        );
        maxY = Math.max(maxY, c.position.y + h);
      }
      // dx/dy > 0：左/上有留白，框右/下移并收缩；< 0：左/上溢出，框左/上移扩大
      const dx = minX - LANE_PAD;
      const dy = minY - (LANE_HEADER_H + LANE_PAD);
      const w = Math.max(maxX - dx + LANE_PAD, TABLE_W + LANE_PAD * 2);
      const h = Math.max(
        maxY - dy + LANE_PAD,
        TABLE_H + LANE_PAD * 2 + LANE_HEADER_H,
      );
      const next = nds.map((n) => {
        if (n.id === laneId && isDatasourceNode(n)) {
          return {
            ...n,
            position: { x: n.position.x + dx, y: n.position.y + dy },
            style: { ...n.style, width: w, height: h },
          };
        }
        if (!isDatasourceNode(n) && n.parentId === laneId) {
          return {
            ...n,
            position: { x: n.position.x - dx, y: n.position.y - dy },
          };
        }
        return n;
      });
      return { next, dx, dy };
    },
    [],
  );

  const onNodeDragStop = useCallback<OnNodeDrag<FlowNode>>(
    (_evt, node) => {
      if (node.id.startsWith("__ds__")) return;
      // 放下后泳道四向归一化；持久化用补偿后的坐标（与画布显示一致）
      let persist = {
        x: Math.round(node.position.x),
        y: Math.round(node.position.y),
      };
      if (node.parentId) {
        const { next, dx, dy } = normalizeLane(nodes, node.parentId);
        persist = {
          x: Math.round(node.position.x - dx),
          y: Math.round(node.position.y - dy),
        };
        setNodes(next);
      }
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(async () => {
        try {
          await apiFetch(`/api/schema-graph/table/${node.id}/position`, {
            method: "PUT",
            body: JSON.stringify(persist),
          });
        } catch (e) {
          // 位置保存失败不影响使用，但必须有观测面（用户下次打开位置丢失会以为是 bug）
          console.warn(
            "[useGraphLayout] 节点位置持久化失败（本次拖拽不落库）",
            e,
          );
        }
      }, 500);
    },
    [nodes, setNodes, normalizeLane],
  );

  return {
    layoutVersion,
    bumpLayoutVersion,
    normalizeLane,
    onNodeDrag,
    onNodeDragStop,
  };
}
