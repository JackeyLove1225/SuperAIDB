"use client";

import { MGMT_API, apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect, useCallback, useRef } from "react";
import Link from "next/link";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

// Management API 地址（通过 Next.js 服务端代理，密钥不出服务器）

// 仪表盘数据缓存键（sessionStorage）：切换路由返回时先渲染缓存，再后台刷新
const CACHE_KEY = "dashboard:cache";

// ── 类型定义 ──

interface DashboardData {
  status: {
    running: boolean;
    pid: number;
    uptime_seconds: number;
    uptime_human: string;
    memory_mb: number;
    cpu_percent: number;
    mcp_ok?: boolean;
    frontend_ok: boolean;
  };
  database: {
    tables: { name: string; rows: number }[];
    table_count: number;
    total_rows: number;
    db_size_mb: number;
  };
  vector_store: {
    collections: { name: string; count: number }[];
    total_collections: number;
    total_vectors: number;
    chroma_size_mb: number;
  };
  logs: {
    timestamp: string;
    level: string;
    logger: string;
    message: string;
  }[];
}

// ── 辅助函数 ──

function formatBytes(mb: number): string {
  if (mb < 1) return `${Math.round(mb * 1024)} KB`;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

const levelColors: Record<string, string> = {
  ERROR: "text-red-600 bg-red-50 dark:bg-red-950 dark:text-red-400",
  WARNING: "text-amber-600 bg-amber-50 dark:bg-amber-950 dark:text-amber-400",
  INFO: "text-zinc-600 bg-zinc-100 dark:bg-zinc-800 dark:text-zinc-400",
  DEBUG: "text-zinc-400 bg-zinc-50 dark:bg-zinc-800 dark:text-zinc-500",
};

// ── 主页面 ──

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [stopping, setStopping] = useState(false);
  // 跟踪组件是否已卸载，避免卸载后 setState
  const mountedRef = useRef(true);
  // 用于在重试期间取消请求
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    // 挂载时先读 sessionStorage 缓存立即渲染，后台 fetch 再刷新（无缓存才显示"加载中"）
    try {
      const cached = sessionStorage.getItem(CACHE_KEY);
      if (cached) {
        setData(JSON.parse(cached) as DashboardData);
        setLoading(false);
      }
    } catch {
      // 缓存损坏时忽略，走正常 fetch
    }
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const fetchData = useCallback(async () => {
    // 取消上一次未完成的请求
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const json = await apiFetch<DashboardData>(
        "/api/dashboard", // apiFetch 自带 MGMT_API 前缀——传全址会双拼 404
        {
          timeoutMs: 8000, // 单次 8s
          retries: 2, // 重试 2 次（共 3 次尝试）
          backoffBaseMs: 500, // 500ms → 1000ms 退避
          externalSignal: controller.signal,
        },
      );
      if (!mountedRef.current) return;
      setData(json);
      setError(null);
      try {
        sessionStorage.setItem(CACHE_KEY, JSON.stringify(json));
      } catch {
        // 存储失败（如超限）不影响主流程
      }
    } catch (e) {
      if (!mountedRef.current) return;
      // AbortError（组件卸载或主动取消）不显示错误
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e.message : "连接失败");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    if (!autoRefresh) return;
    // 30s 刷新（原 5s 过于频繁，/api/dashboard 是重负载聚合端点）
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData, autoRefresh]);

  const clearLogs = async () => {
    try {
      await apiFetch("/api/logs/clear", { method: "POST" });
      fetchData();
    } catch (e) {
      console.warn("清空日志失败:", e);
    }
  };

  const stopBackend = async () => {
    if (!confirm("确定要停止所有后端服务吗？\n这将关闭管理 API 和前端服务。"))
      return;
    setStopping(true);
    setAutoRefresh(false);
    try {
      await apiFetch("/api/stop", { method: "POST" });
      setTimeout(() => {
        setData(null);
        setStopping(false);
      }, 2000);
    } catch {
      setStopping(false);
    }
  };

  if (loading && !data) {
    return (
      <div className="flex h-[calc(100vh-49px)] items-center justify-center">
        <div className="text-zinc-400">加载中...</div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="flex h-[calc(100vh-49px)] flex-col items-center justify-center gap-4">
        <div className="text-red-600">无法连接后端管理服务</div>
        <div className="text-sm text-zinc-500">
          请确认后端已启动（端口 2025）
        </div>
        <div className="text-xs text-zinc-400">错误: {error}</div>
        <button
          onClick={fetchData}
          className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          重试
        </button>
      </div>
    );
  }

  if (!data) return null;

  const s = data.status;
  const db = data.database;
  const vs = data.vector_store;

  return (
    <div className="h-[calc(100vh-49px)] overflow-y-auto bg-white dark:bg-zinc-950">
      <div className="mx-auto max-w-7xl space-y-4 p-6">
        {/* ── 顶部工具栏 ── */}
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            控制台
          </h1>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="rounded accent-zinc-900"
              />
              自动刷新 (30s)
            </label>
            <button
              onClick={fetchData}
              className="rounded-lg border border-zinc-200/70 bg-white px-3 py-1.5 text-sm text-zinc-700 transition-colors hover:bg-zinc-100 dark:border-zinc-700 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              刷新
            </button>
            <button
              onClick={stopBackend}
              disabled={stopping}
              className="rounded-lg border border-red-600/40 bg-white px-3 py-1.5 text-sm font-medium text-red-600 transition-colors hover:bg-red-50 disabled:opacity-50 dark:bg-transparent dark:hover:bg-red-950"
            >
              {stopping ? "停止中..." : "停止后端"}
            </button>
          </div>
        </div>

        {/* ── 后端状态卡片 ── */}
        <div className="rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
          <div className="mb-3 flex items-center gap-2">
            <h2 className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
              后端状态
            </h2>
            <span
              className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
                s.running
                  ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400"
                  : "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  s.running ? "bg-green-500" : "bg-red-500"
                }`}
              />
              {s.running ? "运行中" : "已停止"}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
            <Metric
              label="PID"
              value={s.pid?.toString() || "-"}
            />
            <Metric
              label="运行时间"
              value={s.uptime_human || "-"}
            />
            <Metric
              label="内存占用"
              value={`${s.memory_mb?.toFixed(1) || 0} MB`}
            />
            <Metric
              label="CPU"
              value={`${s.cpu_percent?.toFixed(1) || 0}%`}
            />
            <Metric
              label="MCP 能力面"
              value={s.mcp_ok ? "在线" : "离线"}
            />
            <Metric
              label="前端"
              value={s.frontend_ok ? "在线" : "离线"}
            />
            <Metric
              label="管理端口"
              value=":2025"
            />
          </div>
        </div>

        {/* ── 数据库 + 向量库（并排）── */}
        <div className="grid gap-4 lg:grid-cols-2">
          {/* 数据库概览 */}
          <div className="rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
                数据库概览
              </h2>
              <span className="text-sm text-zinc-500 dark:text-zinc-400">
                {db.table_count} 表 · {db.total_rows} 行 ·{" "}
                {formatBytes(db.db_size_mb)}
              </span>
            </div>
            <div className="space-y-1.5">
              {db.tables.map((t) => (
                <Link
                  key={t.name}
                  href={`/dashboard/tables/${encodeURIComponent(t.name)}`}
                  className="flex items-center justify-between rounded-lg px-3 py-2 transition-colors hover:bg-zinc-100 dark:hover:bg-zinc-800"
                >
                  <span className="font-mono text-sm text-zinc-700 hover:text-zinc-900 hover:underline dark:text-zinc-300 dark:hover:text-zinc-100">
                    {t.name}
                  </span>
                  <span
                    className={`text-sm font-medium tabular-nums ${
                      t.rows > 0
                        ? "text-zinc-900 dark:text-zinc-100"
                        : "text-zinc-400"
                    }`}
                  >
                    {t.rows >= 0 ? `${t.rows} 行` : "—"}
                  </span>
                </Link>
              ))}
              {db.tables.length === 0 && (
                <div className="py-4 text-center text-sm text-zinc-400">
                  暂无数据表
                </div>
              )}
            </div>
          </div>

          {/* 向量数据库 */}
          <div className="rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
                向量数据库
              </h2>
              <span className="text-sm text-zinc-500 dark:text-zinc-400">
                {vs.total_collections} 集合 · {vs.total_vectors} 向量 ·{" "}
                {formatBytes(vs.chroma_size_mb)}
              </span>
            </div>
            <div className="space-y-1.5">
              {vs.collections.map((c) => (
                <div
                  key={c.name}
                  className="flex items-center justify-between rounded-lg px-3 py-2 transition-colors hover:bg-zinc-50 dark:hover:bg-zinc-800"
                >
                  <span className="truncate font-mono text-sm text-zinc-700 dark:text-zinc-300">
                    {c.name}
                  </span>
                  <span
                    className={`text-sm font-medium tabular-nums ${
                      c.count > 0
                        ? "text-zinc-900 dark:text-zinc-100"
                        : "text-zinc-400"
                    }`}
                  >
                    {c.count >= 0 ? `${c.count} 向量` : "—"}
                  </span>
                </div>
              ))}
              {vs.collections.length === 0 && (
                <div className="py-4 text-center text-sm text-zinc-400">
                  暂无文档集合
                </div>
              )}
            </div>
          </div>
        </div>

        {/* ── 表行数可视化（柱状图）── */}
        {db.tables.length > 0 && (
          <div className="rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
            <h2 className="mb-4 text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
              表行数分布
            </h2>
            <ResponsiveContainer
              width="100%"
              height={300}
            >
              <BarChart
                data={db.tables
                  .filter((t) => t.rows > 0)
                  .map((t) => ({ name: t.name, 行数: t.rows }))}
                margin={{ top: 5, right: 30, left: 20, bottom: 5 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  className="opacity-30"
                />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 12, fill: "#71717a" }}
                  angle={-30}
                  textAnchor="end"
                  height={70}
                />
                <YAxis tick={{ fontSize: 12, fill: "#71717a" }} />
                <Tooltip
                  cursor={{ fill: "rgba(0,0,0,0.05)" }}
                  contentStyle={{
                    backgroundColor: "var(--tooltip-bg, #fff)",
                    border: "1px solid #e4e4e7",
                    borderRadius: "8px",
                    fontSize: "13px",
                  }}
                />
                <Bar
                  dataKey="行数"
                  fill="#18181b"
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
            {db.tables.filter((t) => t.rows > 0).length === 0 && (
              <div className="py-8 text-center text-sm text-zinc-400">
                暂无有数据的表
              </div>
            )}
          </div>
        )}

        {/* ── 系统日志 ── */}
        <div className="rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
              系统日志
            </h2>
            <div className="flex items-center gap-2">
              <span className="text-sm text-zinc-500 dark:text-zinc-400">
                最近 {data.logs.length} 条
              </span>
              <button
                onClick={clearLogs}
                className="rounded-lg border border-zinc-200/70 px-2 py-1 text-xs text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-700 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800"
              >
                清空
              </button>
            </div>
          </div>
          <div className="max-h-80 overflow-y-auto rounded-lg border border-zinc-200/70 bg-zinc-50 p-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950">
            {data.logs.length === 0 ? (
              <div className="py-4 text-center text-zinc-400">暂无日志</div>
            ) : (
              [...data.logs].reverse().map((log, i) => (
                <div
                  key={i}
                  className="flex gap-2 border-b border-zinc-100 py-1 last:border-0 dark:border-zinc-900"
                >
                  <span className="shrink-0 text-zinc-400">
                    {log.timestamp}
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-2 text-[11px] font-medium ${
                      levelColors[log.level] || levelColors.INFO
                    }`}
                  >
                    {log.level}
                  </span>
                  <span className="text-zinc-700 dark:text-zinc-300">
                    {log.message}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── 指标组件 ──

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-zinc-50 px-3 py-2.5 dark:bg-zinc-800">
      <div className="text-xs text-zinc-500 dark:text-zinc-400">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tracking-tight text-zinc-900 tabular-nums dark:text-zinc-100">
        {value}
      </div>
    </div>
  );
}
