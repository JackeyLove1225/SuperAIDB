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
  type Connection,
  type OnConnectStart,
  type OnConnectEnd,
  type OnReconnect,
} from "@xyflow/react";

import {
  isDatasourceNode,
  type FlowNode,
  type FlowEdge,
  type GraphNode,
  type GraphData,
  type DeleteTableReport,
  type DeleteRelationshipReport,
  type FlowEdgeData,
} from "@/components/schema-designer/types";
import { apiFetch, ApiError } from "@/lib/api-fetch";
import { useOperatorPassword } from "@/components/ui/operator-password-modal";
import { showError } from "@/components/ui/error-modal";
import { toast } from "sonner";

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
      const other =
        e.source === cur ? e.target : e.target === cur ? e.source : null;
      if (!other) continue;
      keepEdges.add(e.id);
      if (!keepNodes.has(other)) {
        keepNodes.add(other);
        queue.push(other);
      }
    }
  }
  return { keepNodes, keepEdges };
}

// ── 连线 / 删除队列 / 弹窗状态机 / 聚焦淡出 Hook（交互面）──
// graphRef/mountedRef 为数据面持有的共享 ref（读最新图数据 / 预检回写前检查挂载）；
// onAddTableRef 是组合器 ref，见下方 wire effect。

export function useGraphInteractions({
  nodes,
  edges,
  setNodes,
  setEdges,
  fetchGraph,
  fetchStats,
  graphRef,
  mountedRef,
  normalizeLane,
  onAddTableRef,
}: {
  nodes: FlowNode[];
  edges: FlowEdge[];
  setNodes: Dispatch<SetStateAction<FlowNode[]>>;
  setEdges: Dispatch<SetStateAction<FlowEdge[]>>;
  fetchGraph: (forceAutoLayout?: boolean) => Promise<void>;
  fetchStats: () => Promise<void>;
  graphRef: { current: GraphData | null };
  mountedRef: { current: boolean };
  normalizeLane: (
    nds: FlowNode[],
    laneId: string,
  ) => { next: FlowNode[]; dx: number; dy: number };
  onAddTableRef: { current: (datasource: string) => void };
}) {
  // 聚焦模式（点击表卡片）：只亮该表的上下游全链路，其余淡出；点空白清除
  const [focusId, setFocusId] = useState<string | null>(null);
  // hover 淡出（独立于聚焦）：悬停表/边时无关元素淡出
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null);
  const [hoverEdgeId, setHoverEdgeId] = useState<string | null>(null);

  // 模态框状态
  const [showAddTable, setShowAddTable] = useState(false);
  const [showEditTable, setShowEditTable] = useState(false);
  const [editingTable, setEditingTable] = useState<GraphNode | null>(null);
  // 新建表时预填的数据源（从数据源卡片触发时设置）
  const [presetDatasource, setPresetDatasource] = useState<string | null>(null);
  // 高危操作第二因子：删表/删外键确认后弹操作密码框
  const { askPassword, operatorPasswordModal } = useOperatorPassword();

  // ── 删除确认弹窗（影响面预检驱动，替代原生 confirm/直删）：多选按队列逐个弹窗；
  // 预检失败 fail-closed（仅允许取消）。与聊天侧人工闸同一原则：先看影响面再执行。
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

  // 数据源卡片回调（经 React Flow 节点 data 注入）
  const handleAddTable = useCallback(
    (datasource: string) => {
      setPresetDatasource(datasource || null);
      setShowAddTable(true);
    },
    [setPresetDatasource, setShowAddTable],
  );

  // 写入组合器 ref：数据面 fetchGraph 建图注入的是稳定转发器，点击时读取此 ref
  useEffect(() => {
    onAddTableRef.current = handleAddTable;
  }, [onAddTableRef, handleAddTable]);

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
        if (dim === (n.className === "rf-dimmed")) return n;
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
        if (dim === (e.className === "rf-dimmed")) return e;
        changed = true;
        return { ...e, className: dim ? "rf-dimmed" : "" };
      });
      return changed ? out : eds;
    });
  }, [focusId, hoverNodeId, hoverEdgeId, nodes, edges, setNodes, setEdges]);

  // ── 连线：区分数据源→表（新建表）和 表→表（外键关系）──

  const connectingFromDsRef = useRef<string | null>(null);
  const connectHandledRef = useRef(false);

  /** 解析被引用列：取目标表主键（节点 columns 带 is_pk 标志）；画布节点缺列信息时先拉
   * 表详情补齐，都找不到才回退 "id" 并提示（写死 "id" 会对非 id 主键表建出错关系） */
  const resolveRefColumn = useCallback(
    async (targetTable: string): Promise<string> => {
      const findPk = (cols?: GraphNode["columns"]) =>
        cols?.find((c) => c.is_pk)?.name;
      // graphRef 与画布同步刷新（避免 useCallback 闭包拿到旧 nodes）
      const fromGraph = findPk(
        graphRef.current?.nodes.find((n) => n.id === targetTable)?.columns,
      );
      if (fromGraph) return fromGraph;
      try {
        const detail = await apiFetch<{ columns?: GraphNode["columns"] }>(
          `/api/schema-graph/table/${encodeURIComponent(targetTable)}`,
        );
        const fromDetail = findPk(detail.columns);
        if (fromDetail) return fromDetail;
      } catch {
        // 表详情拉取失败按无列信息处理，走下方回退
      }
      toast.warning(`未在 ${targetTable} 表找到主键列，被引用字段回退为 "id"`);
      return "id";
    },
    [graphRef],
  );

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
        setPresetDatasource(connection.source.replace("__ds__", ""));
        setShowAddTable(true);
        return;
      }

      // 场景2：表→表 → 创建外键关系
      // TODO: prompt() 是浏览器原生输入框，toast 无法替代；待做正式对话框后替换
      const colName = prompt(
        `创建外键关系：${connection.source} → ${connection.target}\n请输入 ${connection.source} 表中的外键字段名：`,
        `${connection.target}_id`,
      );
      if (!colName) return;
      // 写皆密码：新建外键关系同样先收集操作密码；取消则中断
      const operatorPassword = await askPassword();
      if (operatorPassword === null) return;
      const refColumn = await resolveRefColumn(connection.target);
      try {
        await apiFetch("/api/schema-graph/relationship", {
          method: "POST",
          body: JSON.stringify({
            table_name: connection.source,
            column_name: colName,
            ref_table_name: connection.target,
            ref_column_name: refColumn,
            operator_password: operatorPassword,
          }),
        });
        fetchGraph();
        fetchStats();
      } catch (e) {
        showError(
          `创建外键失败: ${e instanceof ApiError ? e.message : String(e)}`,
        );
      }
    },
    [fetchGraph, fetchStats, askPassword, resolveRefColumn],
  );

  // 拖到空白处（无目标节点）时触发新建表
  const onConnectEnd = useCallback<OnConnectEnd>((evt) => {
    const dsNodeId = connectingFromDsRef.current;
    connectingFromDsRef.current = null;
    if (!dsNodeId || connectHandledRef.current) return;
    const target = evt?.target as HTMLElement | null;
    if (
      target?.closest(".react-flow__handle") ||
      target?.closest(".react-flow__node")
    )
      return;
    setPresetDatasource(dsNodeId.replace("__ds__", ""));
    setShowAddTable(true);
  }, []);

  // ── 拖边端点（reconnect）：拖到空白=断开（走删外键确认弹窗）；拖到其他表=弹回 ──
  // delete-edge-on-drop：onReconnectStart 标记未落定，onReconnect 复位；onReconnectEnd
  // 时仍未复位即落在空白 → 视为断开。改指向会绕过删外键确认闸，一律弹回。
  const reconnectDoneRef = useRef(true);
  const onReconnectStart = useCallback(() => {
    reconnectDoneRef.current = false;
  }, []);

  const onReconnect = useCallback<OnReconnect<FlowEdge>>((oldEdge, conn) => {
    reconnectDoneRef.current = true;
    if (oldEdge.data?.kind !== "fk") return;
    // 原位放回：无操作（受控状态不更新，边自动保持原样）
    if (conn.source === oldEdge.source && conn.target === oldEdge.target)
      return;
    showError("暂不支持拖动改指向：请先把连线拖到空白处断开，再重新连接。");
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
      // 类型谓词收窄出 fk 边（data.column 只存在于 fk 变体）；先还原画布：边是否删除待弹窗确认
      const fkEdges = deletedEdges.filter(
        (e): e is FlowEdge & { data: Extract<FlowEdgeData, { kind: "fk" }> } =>
          e.data?.kind === "fk",
      );
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
  }, [edgeDelete, mountedRef]);

  const advanceEdgeQueue = useCallback(
    () =>
      setEdgeDelete((s) => {
        if (!s) return s;
        const rest = s.queue.slice(1);
        return rest.length
          ? { queue: rest, report: null, error: null, busy: false }
          : null;
      }),
    [],
  );

  const confirmEdgeDelete = useCallback(async () => {
    const cur = edgeDelete?.queue[0];
    if (!cur) return;
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setEdgeDelete((s) => (s ? { ...s, busy: true } : s));
    try {
      await apiFetch("/api/schema-graph/relationship", {
        method: "DELETE",
        body: JSON.stringify({
          table_name: cur.from_table,
          column_name: cur.from_column,
          operator_password: operatorPassword,
        }),
      });
    } catch (e) {
      showError(
        `删除外键失败: ${e instanceof ApiError ? e.message : String(e)}`,
      );
    }
    advanceEdgeQueue();
    void fetchStats();
    void fetchGraph();
  }, [
    edgeDelete?.queue,
    askPassword,
    advanceEdgeQueue,
    fetchStats,
    fetchGraph,
  ]);

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
    if (
      !tableDelete ||
      tableDelete.busy ||
      tableDelete.report ||
      tableDelete.error
    )
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
          s && s.queue[0] === name
            ? { ...s, error: String(e), busy: false }
            : s,
        );
      }
    })();
  }, [tableDelete, mountedRef]);

  const advanceTableQueue = useCallback(
    () =>
      setTableDelete((s) => {
        if (!s) return s;
        const rest = s.queue.slice(1);
        return rest.length
          ? { queue: rest, report: null, error: null, busy: false }
          : null;
      }),
    [],
  );

  const confirmTableDelete = useCallback(async () => {
    const name = tableDelete?.queue[0];
    if (!name) return;
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setTableDelete((s) => (s ? { ...s, busy: true } : s));
    try {
      await apiFetch(
        `/api/schema-graph/table/${encodeURIComponent(name)}?drop_real_table=true`,
        {
          method: "DELETE",
          body: JSON.stringify({ operator_password: operatorPassword }),
        },
      );
    } catch (e) {
      showError(
        `删除表 ${name} 失败: ${e instanceof ApiError ? e.message : String(e)}`,
      );
    }
    advanceTableQueue();
    void fetchGraph();
    void fetchStats();
  }, [
    tableDelete?.queue,
    askPassword,
    advanceTableQueue,
    fetchGraph,
    fetchStats,
  ]);

  const cancelTableDelete = useCallback(() => setTableDelete(null), []);

  // ── 单击节点：表→切换字段展开 + 设置聚焦；泳道→无操作 ──

  const onNodeClick = useCallback(
    (_evt: React.MouseEvent, node: FlowNode) => {
      if (isDatasourceNode(node)) return;
      setNodes((nds) => {
        const toggled = nds.map((n) =>
          n.id === node.id && !isDatasourceNode(n)
            ? { ...n, data: { ...n.data, showColumns: !n.data.showColumns } }
            : n,
        );
        // 展开/收起改变卡片有效高度，泳道同步四向归一化（边缘卡片展开不溢出）
        return node.parentId
          ? normalizeLane(toggled, node.parentId).next
          : toggled;
      });
      // 聚焦模式：再次点击同一表取消聚焦，否则聚焦该表的上下游
      setFocusId((cur) => (cur === node.id ? null : node.id));
    },
    [setNodes, normalizeLane],
  );

  const onPaneClick = useCallback(() => setFocusId(null), []);
  const onNodeMouseEnter = useCallback(
    (_evt: React.MouseEvent, node: FlowNode) => {
      if (!isDatasourceNode(node)) setHoverNodeId(node.id);
    },
    [],
  );
  const onNodeMouseLeave = useCallback(() => setHoverNodeId(null), []);
  const onEdgeMouseEnter = useCallback(
    (_evt: React.MouseEvent, edge: FlowEdge) => {
      if (edge.data?.kind === "fk") setHoverEdgeId(edge.id);
    },
    [],
  );
  const onEdgeMouseLeave = useCallback(() => setHoverEdgeId(null), []);

  // ── 双击节点：泳道→新建表，表→编辑表 ──
  const onNodeDoubleClick = useCallback(
    (_evt: React.MouseEvent, node: FlowNode) => {
      if (isDatasourceNode(node)) {
        setPresetDatasource(node.id.replace("__ds__", ""));
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
    },
    [],
  );

  return {
    showAddTable,
    setShowAddTable,
    showEditTable,
    setShowEditTable,
    editingTable,
    setEditingTable,
    presetDatasource,
    setPresetDatasource,
    tableDelete,
    edgeDelete,
    confirmTableDelete,
    cancelTableDelete,
    confirmEdgeDelete,
    cancelEdgeDelete,
    operatorPasswordModal,
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
  };
}
