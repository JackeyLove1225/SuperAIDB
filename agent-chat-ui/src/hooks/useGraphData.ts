"use client";

import {
  useState,
  useEffect,
  useCallback,
  useRef,
  type Dispatch,
  type SetStateAction,
} from "react";

import {
  isDatasourceNode,
  type DatasourceFlowNode,
  type FlowNode,
  type FlowEdge,
  type GraphEdge,
  type GraphData,
  type Stats,
  type DatasourceInfo,
} from "@/components/schema-designer/types";
import {
  layoutLaneTables,
  laneContentOffset,
  tableEffectiveHeight,
  TABLE_W,
  TABLE_H,
  LANE_PAD,
  LANE_HEADER_H,
} from "@/components/schema-designer/layout";
import { apiFetch, ApiError } from "@/lib/api-fetch";
import { showError } from "@/components/ui/error-modal";
import { toast } from "sonner";

// ── 辅助函数 ──

/**
 * 后端图边 → React Flow 外键边（归属关系已由泳道容器表达，不再画归属虚线）
 *
 * 边语义：
 * - 卡片中心对中心：source 连接点在引用表左缘，target 在被引用表右缘，
 *   单向直连不绕弯；字段归属信息全部由边标签表达（字段 → 被引用字段 · N:1）
 * - 跨库边：深色虚线 + 标签加"（跨库）"——联邦关系的视觉表达
 */
function buildFkEdges(
  graphEdges: GraphEdge[],
  nodes: GraphData["nodes"],
): FlowEdge[] {
  const dsOf = new Map(nodes.map((n) => [n.id, n.datasource || "primary"]));
  return graphEdges.map((e) => {
    const cross =
      (dsOf.get(e.source) || "primary") !== (dsOf.get(e.target) || "primary");
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      type: "smoothstep",
      animated: true,
      label: `${e.column} → ${e.ref_column} · N:1${cross ? "（跨库）" : ""}`,
      labelStyle: { fontSize: 10, fill: "#71717a" },
      labelBgStyle: { fill: "#f4f4f5" },
      style: cross
        ? { stroke: "#52525b", strokeWidth: 1.8, strokeDasharray: "6 3" }
        : { stroke: "#a1a1aa", strokeWidth: 1.5 },
      data: { kind: "fk", column: e.column, ref_column: e.ref_column, cross },
    } as FlowEdge;
  });
}

// ── 图数据加载 / 轮询 / 统计 / 数据源 / 图库可用性 Hook（数据面）──

export function useGraphData({
  setNodes,
  setEdges,
  onAddTable,
  bumpLayoutVersion,
}: {
  setNodes: Dispatch<SetStateAction<FlowNode[]>>;
  setEdges: Dispatch<SetStateAction<FlowEdge[]>>;
  /** 数据源卡片"新建表"回调（由组合器提供稳定转发器，真实实现挂在交互面） */
  onAddTable: (datasource: string) => void;
  /** 布局版本号 +1（布局面持有 state）：图结构/位置真正变化时驱动画布 remount + fitView */
  bumpLayoutVersion: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [graphEnabled, setGraphEnabled] = useState(false);
  // 图库预热中：图数据来自降级快路径，后台轮询就绪后无缝升级
  const [graphPending, setGraphPending] = useState(false);
  const [datasources, setDatasources] = useState<DatasourceInfo[]>([]);
  const lastFingerprintRef = useRef<string>("");

  // graph_pending 轮询定时器
  const pendingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingAttempts = useRef(0);

  // ── 用 ref 保存最新的 datasources / 图数据，避免 useCallback 闭包陷阱 ──
  const datasourcesRef = useRef<DatasourceInfo[]>([]);
  useEffect(() => {
    datasourcesRef.current = datasources;
  }, [datasources]);

  const graphRef = useRef<GraphData | null>(null);

  // ── 跟踪组件挂载状态 + 取消请求 ──
  const mountedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      if (pendingTimer.current) clearTimeout(pendingTimer.current);
    };
  }, []);

  // ── 数据加载 ──

  const fetchGraph = useCallback(
    async (forceAutoLayout = false) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      if (pendingTimer.current) {
        clearTimeout(pendingTimer.current);
        pendingTimer.current = null;
      }

      try {
        const data = await apiFetch<GraphData>("/api/schema-graph", {
          timeoutMs: 8000,
          retries: 2,
          backoffBaseMs: 500,
          externalSignal: controller.signal,
        });
        if (!mountedRef.current) return;
        graphRef.current = data;

        // 图库预热中：退避重取（3s→6s→…→30s 封顶，最多 20 次——
        // 图库持续不就绪时不再无限 20 次/分钟打服务端）
        if (data.graph_pending) {
          setGraphPending(true);
          pendingAttempts.current += 1;
          if (pendingAttempts.current > 20) {
            console.warn(
              "[useGraphData] 图库预热 20 次未就绪，停止轮询（手动刷新重试）",
            );
            setGraphPending(false);
            return;
          }
          const delay = Math.min(
            3000 * 2 ** (pendingAttempts.current - 1),
            30000,
          );
          pendingTimer.current = setTimeout(() => {
            if (mountedRef.current) void fetchGraph();
          }, delay);
        } else {
          pendingAttempts.current = 0;
          setGraphPending(false);
        }

        const dsList = datasourcesRef.current;

        setNodes((prev) => {
          // 保留泳道拖拽位置与表卡片展开状态（刷新不重置）
          const dsNodeStates: Record<string, { x: number; y: number }> = {};
          const tableExpandStates: Record<string, boolean> = {};
          for (const n of prev) {
            if (isDatasourceNode(n)) {
              dsNodeStates[n.id] = { x: n.position.x, y: n.position.y };
            } else {
              tableExpandStates[n.id] = n.data.showColumns || false;
            }
          }

          // ── 泳道布局：每个数据源一条带形容器，纵向堆叠 ──
          const laneTableIds = new Map<string, string[]>();
          for (const ds of dsList) {
            laneTableIds.set(
              ds.name,
              data.nodes
                .filter((n) => (n.datasource || "primary") === ds.name)
                .map((n) => n.id),
            );
          }
          const laneLayouts = new Map(
            [...laneTableIds.entries()].map(([ds, ids]) => [
              ds,
              layoutLaneTables(ids, data.edges),
            ]),
          );
          const offset = laneContentOffset();

          // ── 位置决策（两阶段）──
          // 先判定每张表的存储位置是否可信（泳道相对坐标语义：非缺省 0,0、
          // 不为越界负值）。只要存在不可信（旧体系绝对坐标/缺省值），整组回退
          // dagre 全排——部分存储 + 部分 dagre 的混排必然视觉混乱；一次自愈为
          // 拓扑布局，持久化后下次加载全部可信，不再回退。
          // 注意：不做"超出 dagre 内容区"判定——用户手动拖远的位置合法，
          // 泳道会按最终位置自适应包住（见下方 finalBoxes）。
          const plausible = (p: { x: number; y: number }) =>
            !(p.x === 0 && p.y === 0) &&
            p.x > -LANE_PAD &&
            p.y >= LANE_HEADER_H &&
            p.x < 20000 &&
            p.y < 20000;
          const anyImplausible = data.nodes.some(
            (n) => !plausible({ x: n.x, y: n.y }),
          );
          const useDagreAll = forceAutoLayout || anyImplausible;

          // 先解出每张表的最终位置
          const finalPos = new Map<string, { x: number; y: number }>();
          for (const n of data.nodes) {
            const dsName = n.datasource || "primary";
            const dagrePos = laneLayouts.get(dsName)?.get(n.id);
            finalPos.set(
              n.id,
              useDagreAll
                ? {
                    x: (dagrePos?.x ?? 0) + offset.x,
                    y: (dagrePos?.y ?? 0) + offset.y,
                  }
                : { x: n.x, y: n.y },
            );
          }

          // 泳道容器按"最终位置"的实际内容尺寸计算——存储位置超出 dagre
          // 内容区时泳道同步变大，卡片绝不溢出泳道可视边界
          const finalBoxes = new Map<
            string,
            { x: number; y: number; width: number; height: number }
          >();
          let cursorY = 0;
          for (const ds of dsList) {
            const ids = laneTableIds.get(ds.name) || [];
            let maxX = TABLE_W;
            let maxY = TABLE_H;
            for (const id of ids) {
              const p = finalPos.get(id)!;
              maxX = Math.max(maxX, p.x + TABLE_W);
              const graphNode = data.nodes.find((gn) => gn.id === id);
              maxY = Math.max(
                maxY,
                p.y +
                  tableEffectiveHeight(
                    (graphNode?.columns || []).length,
                    !!tableExpandStates[id],
                  ),
              );
            }
            const width = Math.max(maxX + LANE_PAD, TABLE_W + LANE_PAD * 2);
            const height = Math.max(
              maxY + LANE_PAD,
              TABLE_H + LANE_PAD * 2 + LANE_HEADER_H,
            );
            finalBoxes.set(ds.name, { x: 0, y: cursorY, width, height });
            cursorY += height + 60;
          }

          const dsNodes: DatasourceFlowNode[] = dsList.map((ds) => {
            const id = `__ds__${ds.name}`;
            const box = finalBoxes.get(ds.name)!;
            const preserved = dsNodeStates[id];
            return {
              id,
              type: "datasource",
              position: preserved || { x: box.x, y: box.y },
              style: { width: box.width, height: box.height },
              data: {
                name: ds.name,
                type: ds.type,
                is_default: ds.is_default,
                table_count: laneTableIds.get(ds.name)?.length ?? 0,
                onAddTable,
              },
              draggable: true,
            } as DatasourceFlowNode;
          });

          const tableNodes: FlowNode[] = data.nodes.map((n) => {
            const dsName = n.datasource || "primary";
            const rel = finalPos.get(n.id)!;
            return {
              id: n.id,
              type: "table",
              parentId: `__ds__${dsName}`,
              // 不加 extent 限制：拖出边界时泳道会自适应扩大包住（onNodeDrag），
              // 放下后泳道收缩到内容贴合（onNodeDragStop）
              position: rel,
              data: {
                name: n.name,
                business_name: n.business_name,
                description: n.description,
                columns: n.columns,
                datasource: dsName,
                showColumns: tableExpandStates[n.id] || false,
              },
            } as FlowNode;
          });

          // 父节点（泳道）必须排在子节点之前（React Flow 要求）
          const all = [...dsNodes, ...tableNodes];
          // dagre 全排（整理布局或自愈回退）时把新位置持久化（尽力而为）
          if (useDagreAll) {
            // 批量持久化：一次 PUT 替代逐表 N 次独立请求（N+1 消除）
            apiFetch("/api/schema-graph/layout", {
              method: "PUT",
              body: JSON.stringify({
                positions: data.nodes.map((n) => {
                  const p = finalPos.get(n.id)!;
                  return {
                    datasource: n.datasource || "primary",
                    table: n.id,
                    x: Math.round(p.x),
                    y: Math.round(p.y),
                  };
                }),
              }),
            }).catch(() => {});
          }
          return all;
        });

        setEdges(buildFkEdges(data.edges, data.nodes));
        // 布局指纹：只有结构/位置真正变化才 bump（触发画布 remount + fitView），
        // 避免 pending 轮询期间无变化时反复重置用户视野
        const fp = JSON.stringify([
          ...data.nodes.map((n) => [n.id, Math.round(n.x), Math.round(n.y)]),
          ...data.edges.map((e) => e.id),
        ]);
        if (fp !== lastFingerprintRef.current) {
          lastFingerprintRef.current = fp;
          bumpLayoutVersion();
        }
        setError(null);
      } catch (e) {
        if (!mountedRef.current) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e.message : "连接失败");
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    },
    [onAddTable, bumpLayoutVersion, setNodes, setEdges],
  );

  const fetchStats = useCallback(async () => {
    try {
      setStats(await apiFetch<Stats>("/api/schema-graph/meta/stats"));
    } catch (e) {
      console.warn("[useGraphData] stats 拉取失败（面板指标降级为不显示）", e);
    }
  }, []);

  const fetchGraphStatus = useCallback(async () => {
    try {
      const data = await apiFetch<{ enabled: boolean; pending?: boolean }>(
        "/api/schema-graph/status",
      );
      setGraphEnabled(data.enabled);
    } catch (e) {
      console.warn("[useGraphData] 图库状态探测失败（按不可用展示）", e);
    }
  }, []);

  const fetchDatasources = useCallback(async () => {
    try {
      const data = await apiFetch<DatasourceInfo[]>(
        "/api/schema-graph/datasources",
      );
      // 空列表兜底默认数据源：保证下方 datasources 变化 effect 必定触发首次 fetchGraph
      setDatasources(
        data.length > 0
          ? data
          : [{ name: "primary", type: "sqlite", is_default: true }],
      );
    } catch {
      // 降级：使用默认数据源
      setDatasources([{ name: "primary", type: "sqlite", is_default: true }]);
    }
  }, []);

  useEffect(() => {
    // 只加载数据源列表/统计/图库状态；首次图加载由下方 datasources 变化
    // effect 触发——原先这里 .then(fetchGraph) 与该 effect 叠加，进页面会发
    // 两次 /api/schema-graph（后者 abort 前者），现已去重为一次
    fetchDatasources().then(() => {
      fetchStats();
      fetchGraphStatus();
    });
  }, [fetchDatasources, fetchStats, fetchGraphStatus]);

  // 数据源列表变化时刷新图（datasources 加载完成后触发，确保泳道渲染）
  useEffect(() => {
    if (datasources.length > 0) {
      fetchGraph();
    }
  }, [datasources, fetchGraph]);

  // ── 同步 YAML ──

  const handleSync = async () => {
    try {
      // timeoutMs: 0 不限时——同步是全量遍历，保持原裸 fetch 无超时的语义
      const data = await apiFetch<{ synced: number; errors: number }>(
        "/api/schema-graph/sync",
        { method: "POST", timeoutMs: 0 },
      );
      toast.success(`同步完成: ${data.synced} 张表, ${data.errors} 个错误`);
      fetchGraph();
      fetchStats();
    } catch (e) {
      showError(`同步失败: ${e instanceof ApiError ? e.message : String(e)}`);
    }
  };

  return {
    loading,
    error,
    stats,
    graphEnabled,
    graphPending,
    datasources,
    fetchGraph,
    fetchStats,
    handleSync,
    // 共享给交互面：主键解析读 graphRef，删除预检 effect 读 mountedRef
    graphRef,
    mountedRef,
  };
}
