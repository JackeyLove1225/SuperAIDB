"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  useNodesState,
  useEdgesState,
  type Connection,
  type OnConnectStart,
  type OnConnectEnd,
  type OnNodeDrag,
  type OnReconnect,
} from "@xyflow/react";

import {
  isDatasourceNode,
  type DatasourceFlowNode,
  type TableFlowNode,
  type FlowNode,
  type FlowEdge,
  type GraphNode,
  type GraphEdge,
  type GraphData,
  type Stats,
  type DatasourceInfo,
  type DeleteTableReport,
  type DeleteRelationshipReport,
  type FlowEdgeData,
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
function buildFkEdges(graphEdges: GraphEdge[], nodes: GraphNode[]): FlowEdge[] {
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

/** 计算聚焦闭包：从某表出发沿外键边双向可达的所有表与边（上游+下游全链路） */
function focusClosure(
  startId: string,
  edges: FlowEdge[],
): { keepNodes: Set<string>; keepEdges: Set<string> } {
  const keepNodes = new Set<string>([startId]);
  const keepEdges = new Set<string>();
  const queue = [startId];
  while (queue.length) {
    const cur = queue.shift()!;
    for (const e of edges) {
      if (e.data?.kind !== "fk") continue;
      let other: string | null = null;
      if (e.source === cur) other = e.target;
      else if (e.target === cur) other = e.source;
      if (other) {
        keepEdges.add(e.id);
        if (!keepNodes.has(other)) {
          keepNodes.add(other);
          queue.push(other);
        }
      }
    }
  }
  return { keepNodes, keepEdges };
}

// ── 图数据加载 / 增删改 / 同步 Hook ──

export function useSchemaGraph() {
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<FlowEdge>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [graphEnabled, setGraphEnabled] = useState(false);
  // 图库预热中：图数据来自降级快路径，后台轮询就绪后无缝升级
  const [graphPending, setGraphPending] = useState(false);
  const [datasources, setDatasources] = useState<DatasourceInfo[]>([]);
  // 布局版本号：图结构/位置真正变化时 +1，驱动画布 remount 并自动 fitView
  const [layoutVersion, setLayoutVersion] = useState(0);
  const lastFingerprintRef = useRef<string>("");

  // 聚焦模式（点击表卡片）：只亮该表的上下游全链路，其余淡出；点空白清除
  const [focusId, setFocusId] = useState<string | null>(null);
  // hover 淡出（独立于我聚焦）：悬停表/边时无关元素淡出
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null);
  const [hoverEdgeId, setHoverEdgeId] = useState<string | null>(null);

  // 模态框状态
  const [showAddTable, setShowAddTable] = useState(false);
  const [showEditTable, setShowEditTable] = useState(false);
  const [editingTable, setEditingTable] = useState<GraphNode | null>(null);
  // 新建表时预填的数据源（从数据源卡片触发时设置）
  const [presetDatasource, setPresetDatasource] = useState<string | null>(null);

  // ── 删除确认弹窗（影响面预检驱动，替代浏览器原生 confirm/无确认直删）──
  // 与聊天侧人工闸同一原则：先看到影响面（行数/引用关系），再决定是否执行。
  // 多选删除按队列逐个弹窗；预检失败 fail-closed（弹窗只允许取消）。
  const [tableDelete, setTableDelete] = useState<{
    queue: string[];
    report: DeleteTableReport | null;
    error: string | null;
    busy: boolean;
  } | null>(null);
  const [edgeDelete, setEdgeDelete] = useState<{
    queue: { from_table: string; from_column: string; to_table: string }[];
    report: DeleteRelationshipReport | null;
    error: string | null;
    busy: boolean;
  } | null>(null);

  // 拖拽位置保存防抖
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // graph_pending 轮询定时器
  const pendingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── 用 ref 保存最新的 datasources / 图数据 / 展开状态，避免 useCallback 闭包陷阱 ──
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

  // 数据源卡片回调（经 React Flow 节点 data 注入）
  const handleAddTable = useCallback((datasource: string) => {
    setPresetDatasource(datasource || null);
    setShowAddTable(true);
  }, [setPresetDatasource, setShowAddTable]);

  const handleClickTable = useCallback((_datasource: string, tableName: string) => {
    if (!tableName) return;
    setNodes((nds) =>
      nds.map((n) => ({ ...n, selected: n.id === tableName })),
    );
    (async () => {
      try {
        const data = await apiFetch<{
          name: string;
          business_name?: string;
          description?: string;
          datasource?: string;
          columns?: GraphNode["columns"];
        }>(`/api/schema-graph/table/${tableName}`);
        setEditingTable({
          id: data.name,
          name: data.name,
          business_name: data.business_name || "",
          description: data.description || "",
          datasource: data.datasource || "primary",
          x: 0,
          y: 0,
          columns: data.columns || [],
        });
        setShowEditTable(true);
      } catch (err) {
        toast.error(`加载表 ${tableName} 失败: ${err}`);
      }
    })();
  }, [setNodes]);

  // ── 数据加载 ──

  const fetchGraph = useCallback(async (forceAutoLayout = false) => {
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

      // 图库预热中：3s 后静默重取，就绪后无缝升级位置/边
      if (data.graph_pending) {
        setGraphPending(true);
        pendingTimer.current = setTimeout(() => {
          if (mountedRef.current) void fetchGraph();
        }, 3000);
      } else {
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
        const anyImplausible = data.nodes.some((n) => !plausible({ x: n.x, y: n.y }));
        const useDagreAll = forceAutoLayout || anyImplausible;

        // 先解出每张表的最终位置
        const finalPos = new Map<string, { x: number; y: number }>();
        for (const n of data.nodes) {
          const dsName = n.datasource || "primary";
          const dagrePos = laneLayouts.get(dsName)?.get(n.id);
          finalPos.set(
            n.id,
            useDagreAll
              ? { x: (dagrePos?.x ?? 0) + offset.x, y: (dagrePos?.y ?? 0) + offset.y }
              : { x: n.x, y: n.y },
          );
        }

        // 泳道容器按"最终位置"的实际内容尺寸计算——存储位置超出 dagre
        // 内容区时泳道同步变大，卡片绝不溢出泳道可视边界
        const finalBoxes = new Map<string, { x: number; y: number; width: number; height: number }>();
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
                  !!tableExpandStates[id]
                )
            );
          }
          const width = Math.max(maxX + LANE_PAD, TABLE_W + LANE_PAD * 2);
          const height = Math.max(maxY + LANE_PAD, TABLE_H + LANE_PAD * 2 + LANE_HEADER_H);
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
              onAddTable: handleAddTable,
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
        setLayoutVersion((v) => v + 1);
      }
      setError(null);
    } catch (e) {
      if (!mountedRef.current) return;
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e.message : "连接失败");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [handleAddTable]);

  const fetchStats = useCallback(async () => {
    try {
      setStats(await apiFetch<Stats>("/api/schema-graph/meta/stats"));
    } catch {
      // 忽略
    }
  }, []);

  const fetchGraphStatus = useCallback(async () => {
    try {
      const data = await apiFetch<{ enabled: boolean; pending?: boolean }>(
        "/api/schema-graph/status",
      );
      setGraphEnabled(data.enabled);
    } catch {
      // 忽略
    }
  }, []);

  const fetchDatasources = useCallback(async () => {
    try {
      const data = await apiFetch<DatasourceInfo[]>("/api/schema-graph/datasources");
      // 空列表兜底默认数据源：保证下方 datasources 变化 effect 必定触发首次 fetchGraph
      setDatasources(
        data.length > 0 ? data : [{ name: "primary", type: "sqlite", is_default: true }],
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

  // ── 聚焦/hover 淡出：计算需要保留的元素，其余加 rf-dimmed 类 ──

  useEffect(() => {
    const active = focusId || hoverNodeId || hoverEdgeId;
    let keepNodes: Set<string> | null = null;
    let keepEdges: Set<string> | null = null;
    if (focusId) {
      ({ keepNodes, keepEdges } = focusClosure(focusId, edges));
    } else if (hoverNodeId) {
      keepNodes = new Set([hoverNodeId]);
      keepEdges = new Set<string>();
      for (const e of edges) {
        if (e.data?.kind !== "fk") continue;
        if (e.source === hoverNodeId || e.target === hoverNodeId) {
          keepEdges.add(e.id);
          keepNodes.add(e.source);
          keepNodes.add(e.target);
        }
      }
    } else if (hoverEdgeId) {
      const e = edges.find((x) => x.id === hoverEdgeId);
      keepNodes = new Set(e ? [e.source, e.target] : []);
      keepEdges = new Set(e ? [e.id] : []);
    }

    setNodes((nds) => {
      let changed = false;
      const out = nds.map((n) => {
        // 泳道容器永不淡出（它是背景）
        const keep = isDatasourceNode(n) || !keepNodes || keepNodes.has(n.id);
        const dim = active ? !keep : false;
        const has = n.className === "rf-dimmed";
        if (dim === has) return n;
        changed = true;
        return { ...n, className: dim ? "rf-dimmed" : "" };
      });
      return changed ? out : nds;
    });
    setEdges((eds) => {
      let changed = false;
      const out = eds.map((e) => {
        const keep = !keepEdges || keepEdges.has(e.id);
        const dim = active ? !keep : false;
        const has = e.className === "rf-dimmed";
        if (dim === has) return e;
        changed = true;
        return { ...e, className: dim ? "rf-dimmed" : "" };
      });
      return changed ? out : eds;
    });
  }, [focusId, hoverNodeId, hoverEdgeId, nodes, edges, setNodes, setEdges]);

  // ── 整理布局：dagre 全量重排并持久化 ──

  const handleAutoLayout = useCallback(() => {
    void fetchGraph(true);
  }, [fetchGraph]);

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
              !!node.data.showColumns
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
        })
      );
    },
    [setNodes]
  );

  /**
   * 泳道四向归一化：左/上方向平移泳道框并反向补偿全部子卡片
   * （卡片视觉位置不变，边框跟上），右/下方向按内容收尺寸。
   * 返回归一化后的节点数组与平移量（用于把被拖卡片的补偿后坐标持久化）。
   */
  const normalizeLane = useCallback(
    (nds: FlowNode[], laneId: string): { next: FlowNode[]; dx: number; dy: number } => {
      const lane = nds.find((n) => n.id === laneId);
      if (!lane || !isDatasourceNode(lane)) return { next: nds, dx: 0, dy: 0 };
      const children = nds.filter(
        (n): n is TableFlowNode => !isDatasourceNode(n) && n.parentId === laneId
      );
      if (children.length === 0) return { next: nds, dx: 0, dy: 0 };
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const c of children) {
        minX = Math.min(minX, c.position.x);
        minY = Math.min(minY, c.position.y);
        maxX = Math.max(maxX, c.position.x + TABLE_W);
        // 高度按有效高度：展开的卡片按字段数估算（展开边缘卡片不溢出下边框）
        const h = tableEffectiveHeight(
          (c.data.columns || []).length,
          !!c.data.showColumns
        );
        maxY = Math.max(maxY, c.position.y + h);
      }
      // dx/dy > 0：左/上有留白，框右/下移并收缩；< 0：左/上溢出，框左/上移扩大
      const dx = minX - LANE_PAD;
      const dy = minY - (LANE_HEADER_H + LANE_PAD);
      const w = Math.max(maxX - dx + LANE_PAD, TABLE_W + LANE_PAD * 2);
      const h = Math.max(maxY - dy + LANE_PAD, TABLE_H + LANE_PAD * 2 + LANE_HEADER_H);
      const next = nds.map((n) => {
        if (n.id === laneId && isDatasourceNode(n)) {
          return {
            ...n,
            position: { x: n.position.x + dx, y: n.position.y + dy },
            style: { ...n.style, width: w, height: h },
          };
        }
        if (!isDatasourceNode(n) && n.parentId === laneId) {
          return { ...n, position: { x: n.position.x - dx, y: n.position.y - dy } };
        }
        return n;
      });
      return { next, dx, dy };
    },
    []
  );

  const onNodeDragStop = useCallback<OnNodeDrag<FlowNode>>(
    (_evt, node) => {
      if (node.id.startsWith("__ds__")) return;
      // 放下后泳道四向归一化；持久化用补偿后的坐标（与画布显示一致）
      let persist = { x: Math.round(node.position.x), y: Math.round(node.position.y) };
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
        } catch {
          // 位置保存失败不影响使用
        }
      }, 500);
    },
    [nodes, setNodes, normalizeLane]
  );

  // ── 连线：区分数据源→表（新建表）和 表→表（外键关系）──

  const connectingFromDsRef = useRef<string | null>(null);
  const connectHandledRef = useRef(false);

  /** 解析被引用列：取目标表主键（节点 columns 带 is_pk 标志）。
   * 画布节点缺列信息时先拉表详情补齐；都找不到才回退 "id" 并提示——
   * 写死 "id" 会对 uid 等非 id 主键的表建出外键指向不存在列的错关系 */
  const resolveRefColumn = useCallback(async (targetTable: string): Promise<string> => {
    const findPk = (cols?: GraphNode["columns"]) => cols?.find((c) => c.is_pk)?.name;
    // graphRef 与画布同步刷新（避免 useCallback 闭包拿到旧 nodes）
    const fromGraph = findPk(
      graphRef.current?.nodes.find((n) => n.id === targetTable)?.columns
    );
    if (fromGraph) return fromGraph;
    try {
      const detail = await apiFetch<{ columns?: GraphNode["columns"] }>(
        `/api/schema-graph/table/${encodeURIComponent(targetTable)}`
      );
      const fromDetail = findPk(detail.columns);
      if (fromDetail) return fromDetail;
    } catch {
      // 表详情拉取失败按无列信息处理，走下方回退
    }
    toast.warning(`未在 ${targetTable} 表找到主键列，被引用字段回退为 "id"`);
    return "id";
  }, []);

  const onConnectStart = useCallback<OnConnectStart>((_evt, params) => {
    const nodeId = params?.nodeId || "";
    connectingFromDsRef.current = nodeId.startsWith("__ds__") ? nodeId : null;
    connectHandledRef.current = false;
  }, []);

  const onConnect = useCallback(
    async (connection: Connection) => {
      connectHandledRef.current = true;
      if (!connection.source || !connection.target) return;

      // 场景1：从数据源泳道拖出连接到表 → 触发新建表
      if (connection.source.startsWith("__ds__")) {
        const dsName = connection.source.replace("__ds__", "");
        setPresetDatasource(dsName);
        setShowAddTable(true);
        return;
      }

      // 场景2：表→表 → 创建外键关系
      // TODO: prompt() 是浏览器原生输入框，toast 无法替代；待做正式对话框后替换
      const colName = prompt(
        `创建外键关系：${connection.source} → ${connection.target}\n请输入 ${connection.source} 表中的外键字段名：`,
        `${connection.target}_id`
      );
      if (!colName) return;
      const refColumn = await resolveRefColumn(connection.target);
      try {
        await apiFetch("/api/schema-graph/relationship", {
          method: "POST",
          body: JSON.stringify({
            table_name: connection.source,
            column_name: colName,
            ref_table_name: connection.target,
            ref_column_name: refColumn,
          }),
        });
        fetchGraph();
        fetchStats();
      } catch (e) {
        toast.error(`创建外键失败: ${e instanceof ApiError ? e.message : String(e)}`);
      }
    },
    [fetchGraph, fetchStats, resolveRefColumn]
  );

  // 拖到空白处（无目标节点）时触发新建表
  const onConnectEnd = useCallback<OnConnectEnd>((evt) => {
    const dsNodeId = connectingFromDsRef.current;
    connectingFromDsRef.current = null;
    if (!dsNodeId || connectHandledRef.current) return;
    const target = evt?.target as HTMLElement | null;
    if (target?.closest(".react-flow__handle") || target?.closest(".react-flow__node")) {
      return;
    }
    const dsName = dsNodeId.replace("__ds__", "");
    setPresetDatasource(dsName);
    setShowAddTable(true);
  }, []);

  // ── 拖边端点（reconnect）：拖到空白=断开（走删外键确认弹窗）；拖到其他表=弹回 ──
  // 官方 delete-edge-on-drop 模式：onReconnectStart 标记未落定，
  // 落到有效连接点会触发 onReconnect 复位标记；onReconnectEnd 时标记仍未复位
  // 即落在空白 → 视为断开。改指向=删旧外键+建新外键，删外键有影响面确认闸，
  // 拖动链路不允许静默绕过，故改指向一律弹回并提示。

  const reconnectDoneRef = useRef(true);

  const onReconnectStart = useCallback(() => {
    reconnectDoneRef.current = false;
  }, []);

  const onReconnect = useCallback<OnReconnect<FlowEdge>>((oldEdge, conn) => {
    reconnectDoneRef.current = true;
    if (oldEdge.data?.kind !== "fk") return;
    // 原位放回：无操作（受控状态不更新，边自动保持原样）
    if (conn.source === oldEdge.source && conn.target === oldEdge.target) return;
    toast.error("暂不支持拖动改指向：请先把连线拖到空白处断开，再重新连接。");
  }, []);

  const onReconnectEnd = useCallback(
    (_evt: MouseEvent | TouchEvent, edge: FlowEdge) => {
      const droppedOnEmpty = !reconnectDoneRef.current;
      reconnectDoneRef.current = true;
      if (!droppedOnEmpty || edge.data?.kind !== "fk") return;
      // 与按 Delete 键同一链路：影响面预检弹窗确认后才真正删除；
      // 取消则边保持原状（reconnect 未改动受控状态，无需额外还原）
      setEdgeDelete({
        queue: [
          {
            from_table: edge.source,
            from_column: edge.data.column,
            to_table: edge.target,
          },
        ],
        report: null,
        error: null,
        busy: false,
      });
    },
    [],
  );

  // ── 删除边：挂起 → 影响面预检弹窗 → 确认才删（原直删无确认，20260805 补齐）──

  const onEdgesDelete = useCallback(
    (deletedEdges: FlowEdge[]) => {
      // 类型谓词收窄出 fk 边（data.column 只存在于 fk 变体）
      const fkEdges = deletedEdges.filter(
        (e): e is FlowEdge & { data: Extract<FlowEdgeData, { kind: "fk" }> } =>
          e.data?.kind === "fk",
      );
      // 先还原画布：边是否删除待弹窗确认，确认前视觉保持原状
      void fetchGraph();
      if (fkEdges.length === 0) return;
      setEdgeDelete({
        queue: fkEdges.map((e) => ({
          from_table: e.source,
          from_column: e.data.column,
          to_table: e.target,
        })),
        report: null,
        error: null,
        busy: false,
      });
    },
    [fetchGraph],
  );

  // 删边弹窗打开即对队首做影响面预检；失败 fail-closed（仅允许取消）
  useEffect(() => {
    if (!edgeDelete || edgeDelete.busy || edgeDelete.report || edgeDelete.error)
      return;
    const cur = edgeDelete.queue[0];
    if (!cur) {
      setEdgeDelete(null);
      return;
    }
    setEdgeDelete((s) => (s ? { ...s, busy: true } : s));
    (async () => {
      try {
        const data = await apiFetch<{ report?: DeleteRelationshipReport }>(
          "/api/schema-graph/relationship/delete/precheck",
          {
            method: "POST",
            body: JSON.stringify({
              from_table: cur.from_table,
              from_column: cur.from_column,
            }),
          },
        );
        if (!mountedRef.current) return;
        setEdgeDelete((s) =>
          s && s.queue[0] === cur
            ? { ...s, report: data.report ?? null, busy: false }
            : s,
        );
      } catch (e) {
        if (!mountedRef.current) return;
        setEdgeDelete((s) =>
          s && s.queue[0] === cur ? { ...s, error: String(e), busy: false } : s,
        );
      }
    })();
  }, [edgeDelete]);

  const advanceEdgeQueue = useCallback(() => {
    setEdgeDelete((s) => {
      if (!s) return s;
      const rest = s.queue.slice(1);
      return rest.length
        ? { queue: rest, report: null, error: null, busy: false }
        : null;
    });
  }, []);

  const confirmEdgeDelete = useCallback(async () => {
    const cur = edgeDelete?.queue[0];
    if (!cur) return;
    setEdgeDelete((s) => (s ? { ...s, busy: true } : s));
    try {
      await apiFetch("/api/schema-graph/relationship", {
        method: "DELETE",
        body: JSON.stringify({
          table_name: cur.from_table,
          column_name: cur.from_column,
        }),
      });
    } catch (e) {
      toast.error(`删除外键失败: ${e instanceof ApiError ? e.message : String(e)}`);
    }
    advanceEdgeQueue();
    void fetchStats();
    void fetchGraph();
  }, [edgeDelete?.queue, advanceEdgeQueue, fetchStats, fetchGraph]);

  const cancelEdgeDelete = useCallback(() => setEdgeDelete(null), []);

  // ── 删除节点：只删除表节点，泳道不可删除；影响面预检弹窗确认（原一行原生 confirm）──

  const onNodesDelete = useCallback(
    (deletedNodes: FlowNode[]) => {
      const tableNodes = deletedNodes.filter((n) => !n.id.startsWith("__ds__"));
      // 先还原画布：节点是否删除待弹窗确认，确认前视觉保持原状
      void fetchGraph();
      if (tableNodes.length === 0) return;
      setTableDelete({
        queue: tableNodes.map((n) => n.id),
        report: null,
        error: null,
        busy: false,
      });
    },
    [fetchGraph],
  );

  // 删表弹窗打开即对队首做影响面预检；失败 fail-closed（仅允许取消）
  useEffect(() => {
    if (!tableDelete || tableDelete.busy || tableDelete.report || tableDelete.error)
      return;
    const name = tableDelete.queue[0];
    if (!name) {
      setTableDelete(null);
      return;
    }
    setTableDelete((s) => (s ? { ...s, busy: true } : s));
    (async () => {
      try {
        const data = await apiFetch<{ report?: DeleteTableReport }>(
          `/api/schema-graph/table/${encodeURIComponent(name)}/delete/precheck`,
          { method: "POST" },
        );
        if (!mountedRef.current) return;
        setTableDelete((s) =>
          s && s.queue[0] === name
            ? { ...s, report: data.report ?? null, busy: false }
            : s,
        );
      } catch (e) {
        if (!mountedRef.current) return;
        setTableDelete((s) =>
          s && s.queue[0] === name ? { ...s, error: String(e), busy: false } : s,
        );
      }
    })();
  }, [tableDelete]);

  const advanceTableQueue = useCallback(() => {
    setTableDelete((s) => {
      if (!s) return s;
      const rest = s.queue.slice(1);
      return rest.length
        ? { queue: rest, report: null, error: null, busy: false }
        : null;
    });
  }, []);

  const confirmTableDelete = useCallback(async () => {
    const name = tableDelete?.queue[0];
    if (!name) return;
    setTableDelete((s) => (s ? { ...s, busy: true } : s));
    try {
      await apiFetch(
        `/api/schema-graph/table/${encodeURIComponent(name)}?drop_real_table=true`,
        { method: "DELETE" },
      );
    } catch (e) {
      toast.error(`删除表 ${name} 失败: ${e instanceof ApiError ? e.message : String(e)}`);
    }
    advanceTableQueue();
    void fetchGraph();
    void fetchStats();
  }, [tableDelete?.queue, advanceTableQueue, fetchGraph, fetchStats]);

  const cancelTableDelete = useCallback(() => setTableDelete(null), []);

  // ── 单击节点：表→切换字段展开 + 设置聚焦；泳道→无操作 ──

  const onNodeClick = useCallback((_evt: React.MouseEvent, node: FlowNode) => {
    if (isDatasourceNode(node)) return;
    setNodes((nds) => {
      const toggled = nds.map((n) => {
        if (n.id === node.id && !isDatasourceNode(n)) {
          return { ...n, data: { ...n.data, showColumns: !n.data.showColumns } };
        }
        return n;
      });
      // 展开/收起改变卡片有效高度，泳道同步四向归一化（边缘卡片展开不溢出）
      return node.parentId ? normalizeLane(toggled, node.parentId).next : toggled;
    });
    // 聚焦模式：再次点击同一表取消聚焦，否则聚焦该表的上下游
    setFocusId((cur) => (cur === node.id ? null : node.id));
  }, [setNodes, normalizeLane]);

  const onPaneClick = useCallback(() => {
    setFocusId(null);
  }, []);

  // ── hover 淡出 ──

  const onNodeMouseEnter = useCallback((_evt: React.MouseEvent, node: FlowNode) => {
    if (!isDatasourceNode(node)) setHoverNodeId(node.id);
  }, []);
  const onNodeMouseLeave = useCallback(() => setHoverNodeId(null), []);
  const onEdgeMouseEnter = useCallback((_evt: React.MouseEvent, edge: FlowEdge) => {
    if (edge.data?.kind === "fk") setHoverEdgeId(edge.id);
  }, []);
  const onEdgeMouseLeave = useCallback(() => setHoverEdgeId(null), []);

  // ── 双击节点：泳道→新建表，表→编辑表 ──

  const onNodeDoubleClick = useCallback((_evt: React.MouseEvent, node: FlowNode) => {
    if (isDatasourceNode(node)) {
      const dsName = node.id.replace("__ds__", "");
      setPresetDatasource(dsName);
      setShowAddTable(true);
      return;
    }
    const graphNode: GraphNode = {
      id: node.id,
      name: node.data.name,
      business_name: node.data.business_name || "",
      description: node.data.description || "",
      datasource: node.data.datasource || "primary",
      x: node.position.x,
      y: node.position.y,
      columns: node.data.columns || [],
    };
    setEditingTable(graphNode);
    setShowEditTable(true);
  }, []);

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
      toast.error(`同步失败: ${e instanceof ApiError ? e.message : String(e)}`);
    }
  };

  return {
    // 画布状态
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    // 数据状态
    loading,
    error,
    stats,
    graphEnabled,
    graphPending,
    datasources,
    layoutVersion,
    // 模态框状态
    showAddTable,
    setShowAddTable,
    showEditTable,
    setShowEditTable,
    editingTable,
    setEditingTable,
    presetDatasource,
    setPresetDatasource,
    // 删除确认弹窗（影响面预检驱动）
    tableDelete,
    edgeDelete,
    confirmTableDelete,
    cancelTableDelete,
    confirmEdgeDelete,
    cancelEdgeDelete,
    // 数据加载
    fetchGraph,
    fetchStats,
    // 画布交互
    onNodeDrag,
    onNodeDragStop,
    onConnectStart,
    onConnect,
    onConnectEnd,
    onReconnectStart,
    onReconnect,
    onReconnectEnd,
    onEdgesDelete,
    onNodesDelete,
    onNodeClick,
    onNodeDoubleClick,
    onPaneClick,
    onNodeMouseEnter,
    onNodeMouseLeave,
    onEdgeMouseEnter,
    onEdgeMouseLeave,
    // 操作
    handleSync,
    handleAutoLayout,
  };
}
