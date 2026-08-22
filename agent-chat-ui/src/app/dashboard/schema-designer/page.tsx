"use client";

import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type NodeTypes,
  BackgroundVariant,
  ConnectionMode,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import TableNode from "@/components/schema-designer/table-node";
import DatasourceNode from "@/components/schema-designer/datasource-node";
import TableEditorModal from "@/components/schema-designer/TableEditorModal";
import ImpactConfirmDialog, {
  type ImpactSection,
} from "@/components/schema-designer/ImpactConfirmDialog";
import type {
  FlowNode,
  FlowEdge,
  DeleteTableReport,
  DeleteRelationshipReport,
} from "@/components/schema-designer/types";
import { useSchemaGraph } from "@/hooks/useSchemaGraph";

// ── React Flow 节点类型注册（必须在组件外定义避免无限重建）──

const nodeTypes: NodeTypes = { table: TableNode, datasource: DatasourceNode };

// ── 删除预检报告 → 弹窗分区（与聊天侧核武卡同一信息结构）──

function tableDeleteSections(r: DeleteTableReport): ImpactSection[] {
  return [
    {
      label: "记录",
      lines: [
        r.row_count === null
          ? "库中无此表或统计失败（仅删除元数据）"
          : `${r.row_count} 行记录将随表删除`,
      ],
    },
    {
      label: "本表外键（随之删除）",
      lines: r.outgoing_fks.length
        ? r.outgoing_fks.map((fk) => `${fk.column} → ${fk.references}.${fk.ref_column}`)
        : ["无"],
    },
    {
      label: "被引用（连带影响）",
      lines: r.referenced_by.length
        ? r.referenced_by.map((ref) =>
            ref.rows === null
              ? `⚠ ${ref.table}.${ref.column} → 引用行数统计失败`
              : ref.rows > 0
                ? `⚠ ${ref.table}.${ref.column} → ${ref.rows} 行引用了本表记录`
                : `${ref.table}.${ref.column} → 0 行（无影响）`,
          )
        : ["无表引用本表"],
    },
  ];
}

function edgeDeleteSections(r: DeleteRelationshipReport): ImpactSection[] {
  return [
    {
      label: "引用关系",
      lines: [`${r.table}.${r.column} → ${r.references}.${r.ref_column}`],
    },
    {
      label: "受影响数据",
      lines: [
        r.affected_rows === null
          ? "⚠ 受影响行数统计失败"
          : `${r.affected_rows} 行现有数据将失去引用约束保护`,
      ],
    },
  ];
}

// ── 主页面 ──

export default function SchemaDesignerPage() {
  const {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    loading,
    error,
    stats,
    graphEnabled,
    graphPending,
    datasources,
    layoutVersion,
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
    fetchGraph,
    fetchStats,
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
    handleSync,
    handleAutoLayout,
  } = useSchemaGraph();

  // ── 渲染 ──

  if (loading) {
    return (
      <div className="flex h-[calc(100vh-49px)] items-center justify-center">
        <div className="text-zinc-400">加载表结构设计器...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-[calc(100vh-49px)] flex-col items-center justify-center gap-4">
        <div className="text-red-600">无法连接后端管理服务</div>
        <div className="text-sm text-zinc-500">请确认后端已启动（端口 2025）</div>
        <div className="text-xs text-zinc-400">错误: {error}</div>
        <button
          onClick={() => fetchGraph()}
          className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700"
        >
          重试
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-49px)] flex-col bg-white dark:bg-zinc-950">
      {/* ── 顶部工具栏 ── */}
      <div className="flex items-center justify-between border-b border-zinc-200/70 bg-white px-4 py-2 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center gap-2">
          <h1 className="mr-4 text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            表结构设计器
          </h1>
          <button
            onClick={() => fetchGraph()}
            className="rounded-lg border border-zinc-200/70 bg-white px-3 py-1.5 text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            刷新
          </button>
          <button
            onClick={() => setShowAddTable(true)}
            className="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-zinc-700"
          >
            新建表
          </button>
          <button
            onClick={handleSync}
            className="rounded-lg border border-zinc-200/70 bg-white px-3 py-1.5 text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            同步 YAML
          </button>
          <button
            onClick={handleAutoLayout}
            className="rounded-lg border border-zinc-200/70 bg-white px-3 py-1.5 text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800"
            title="按外键拓扑自动分层重排：主表在左，引用方在右"
          >
            整理布局
          </button>
        </div>
        <div className="flex items-center gap-4 text-xs text-zinc-500">
          {graphPending && (
            <span className="flex items-center gap-1 text-amber-600">
              <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
              图数据库启动中，当前为自动布局…
            </span>
          )}
          {stats && (
            <span className="tabular-nums">
              {stats.table_count} 表 / {stats.column_count} 字段 / {stats.relationship_count} 关系
            </span>
          )}
          <span className={`flex items-center gap-1 ${graphEnabled ? "text-green-600" : "text-zinc-400"}`}>
            <span className={`h-2 w-2 rounded-full ${graphEnabled ? "bg-green-500" : "bg-zinc-400"}`} />
            图库 {graphEnabled ? "已连接" : graphPending ? "启动中" : "降级模式"}
          </span>
        </div>
      </div>

      {/* ── React Flow 画布 ── */}
      <div className="relative flex-1">
        {/* key 绑定布局版本：结构/位置变化时 remount 并触发 fitView，
            比编程式 fitView 更可靠（节点测量完成后再拟合） */}
        <ReactFlow<FlowNode, FlowEdge>
          key={`gv-${layoutVersion}`}
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectStart={onConnectStart}
          onConnectEnd={onConnectEnd}
          onReconnect={onReconnect}
          onReconnectStart={onReconnectStart}
          onReconnectEnd={onReconnectEnd}
          onNodeClick={onNodeClick}
          onNodeDrag={onNodeDrag}
          onNodeDragStop={onNodeDragStop}
          onNodesDelete={onNodesDelete}
          onEdgesDelete={onEdgesDelete}
          onNodeDoubleClick={onNodeDoubleClick}
          onPaneClick={onPaneClick}
          onNodeMouseEnter={onNodeMouseEnter}
          onNodeMouseLeave={onNodeMouseLeave}
          onEdgeMouseEnter={onEdgeMouseEnter}
          onEdgeMouseLeave={onEdgeMouseLeave}
          nodeTypes={nodeTypes}
          connectionMode={ConnectionMode.Loose}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.2}
          maxZoom={2}
          defaultEdgeOptions={{
            type: "smoothstep",
            markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "#a1a1aa" },
          }}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
          <Controls />
          <MiniMap
            nodeColor={(n) => (n.selected ? "#18181b" : "#d4d4d8")}
            maskColor="rgba(0,0,0,0.05)"
            className="!bg-zinc-100 dark:!bg-zinc-800"
          />
        </ReactFlow>

        {/* 空状态提示 */}
        {nodes.length === 0 && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <div className="text-center">
              <div className="text-lg font-medium tracking-tight text-zinc-400">画布为空</div>
              <div className="mt-1 text-sm text-zinc-400">
                点击「新建表」手动创建；想让 AI 帮你设计？去聊天页上传文件说「建成数据库」
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ── 模态框 ── */}
      {showAddTable && (
        <TableEditorModal
          mode="create"
          presetDatasource={presetDatasource}
          datasources={datasources}
          onClose={() => {
            setShowAddTable(false);
            setPresetDatasource(null);
          }}
          onSaved={() => {
            setShowAddTable(false);
            setPresetDatasource(null);
            fetchGraph();
            fetchStats();
          }}
        />
      )}
      {showEditTable && editingTable && (
        <TableEditorModal
          mode="edit"
          initialData={editingTable}
          datasources={datasources}
          onClose={() => {
            setShowEditTable(false);
            setEditingTable(null);
          }}
          onSaved={() => {
            setShowEditTable(false);
            setEditingTable(null);
            fetchGraph();
            fetchStats();
          }}
        />
      )}

      {/* ── 删除确认弹窗（影响面预检驱动）── */}
      {tableDelete && (
        <ImpactConfirmDialog
          title={`删除表 ${tableDelete.queue[0] ?? ""}${tableDelete.queue.length > 1 ? `（共 ${tableDelete.queue.length} 张，逐个确认）` : ""}`}
          loading={!tableDelete.report && !tableDelete.error}
          error={tableDelete.error}
          busy={tableDelete.busy}
          sections={tableDelete.report ? tableDeleteSections(tableDelete.report) : []}
          warning="删除后不可恢复：YAML 定义、元数据与实际数据库表将一并删除；存在反向引用时真实库删除会被契约拦截并提示。"
          onConfirm={confirmTableDelete}
          onCancel={cancelTableDelete}
        />
      )}
      {edgeDelete && (
        <ImpactConfirmDialog
          title={`删除外键 ${edgeDelete.queue[0] ? `${edgeDelete.queue[0].from_table}.${edgeDelete.queue[0].from_column}` : ""}${edgeDelete.queue.length > 1 ? `（共 ${edgeDelete.queue.length} 条，逐个确认）` : ""}`}
          loading={!edgeDelete.report && !edgeDelete.error}
          error={edgeDelete.error}
          busy={edgeDelete.busy}
          sections={edgeDelete.report ? edgeDeleteSections(edgeDelete.report) : []}
          warning="仅解除元数据层引用关系，数据不丢；但解除后对目标表的删改不再触发引用护栏。"
          onConfirm={confirmEdgeDelete}
          onCancel={cancelEdgeDelete}
        />
      )}
    </div>
  );
}
