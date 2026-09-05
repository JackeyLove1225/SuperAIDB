"use client";

import { useCallback, useRef } from "react";
import { useNodesState, useEdgesState } from "@xyflow/react";

import {
  type FlowNode,
  type FlowEdge,
} from "@/components/schema-designer/types";
import { useGraphData } from "@/hooks/useGraphData";
import { useGraphLayout } from "@/hooks/useGraphLayout";
import { useGraphInteractions } from "@/hooks/useGraphInteractions";

// ── 图数据加载 / 增删改 / 同步 Hook（组合器：数据面 + 布局面 + 交互面）──
// 对外返回对象形状与拆分前完全一致，page.tsx 无需改动。

export function useSchemaGraph() {
  // 画布节点/边状态为三面共享，由组合器持有并下发
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<FlowEdge>([]);

  // 数据面 fetchGraph 建图时需注入"新建表"回调，而该回调的实现属于交互面
  // （依赖 showAddTable/presetDatasource 状态）——存在 数据面↔交互面 循环依赖，
  // 用 ref 桥打破：组合器提供稳定转发器，交互面挂载后把真实实现写入 ref。
  // fetchGraph 只在挂载后的 effect/轮询中运行、回调只在用户点击时触发，
  // 读取时 ref 必定已就绪，与原直接闭包行为一致。
  const handleAddTableRef = useRef<(datasource: string) => void>(() => {});
  const onAddTable = useCallback((datasource: string) => {
    handleAddTableRef.current(datasource);
  }, []);

  const {
    layoutVersion,
    bumpLayoutVersion,
    normalizeLane,
    onNodeDrag,
    onNodeDragStop,
  } = useGraphLayout({ nodes, setNodes });

  const {
    loading,
    error,
    stats,
    graphEnabled,
    graphPending,
    datasources,
    fetchGraph,
    fetchStats,
    handleSync,
    graphRef,
    mountedRef,
  } = useGraphData({ setNodes, setEdges, onAddTable, bumpLayoutVersion });

  // 整理布局 = 强制 dagre 全排的数据重取（算法应用在 fetchGraph 内，触发器属布局面）
  const handleAutoLayout = useCallback(() => {
    void fetchGraph(true);
  }, [fetchGraph]);

  const {
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
  } = useGraphInteractions({
    nodes,
    edges,
    setNodes,
    setEdges,
    fetchGraph,
    fetchStats,
    graphRef,
    mountedRef,
    normalizeLane,
    onAddTableRef: handleAddTableRef,
  });

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
    // 操作密码确认弹窗（高危操作第二因子，fixed 定位，页面渲染即可）
    operatorPasswordModal,
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
