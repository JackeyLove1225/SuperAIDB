"use client";

import { MGMT_API, apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect, useCallback } from "react";

// 通过 Next.js 服务端代理访问 Management API（密钥不出服务器）

interface Datasource {
  name: string;
  type: string;
  is_default: boolean;
  host: string;
  database: string;
  table_count: number;
}

interface TestResult {
  ok: boolean;
  message: string;
  tables?: number;
}

export default function DatasourcesPage() {
  const [datasources, setDatasources] = useState<Datasource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, TestResult>>(
    {},
  );
  const [testing, setTesting] = useState<string | null>(null);
  const [tableDetails, setTableDetails] = useState<
    Record<string, { name: string; rows: number }[]>
  >({});
  const [tableErrors, setTableErrors] = useState<Record<string, string>>({});

  const fetchData = useCallback(async () => {
    try {
      const data = await apiFetch<{ datasources?: Datasource[] }>(
        "/api/datasources",
        {
          timeoutMs: 10000,
        },
      );
      setDatasources(data.datasources || []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const testConnection = async (name: string) => {
    setTesting(name);
    try {
      const data = await apiFetch<TestResult>(`/api/datasources/${name}/test`);
      setTestResults((prev) => ({ ...prev, [name]: data }));
    } catch (e) {
      setTestResults((prev) => ({
        ...prev,
        [name]: {
          ok: false,
          message: e instanceof Error ? e.message : "请求失败",
        },
      }));
    } finally {
      setTesting(null);
    }
  };

  const loadTables = async (name: string) => {
    try {
      const data = await apiFetch<{
        tables?: { name: string; rows: number }[];
      }>(`/api/datasources/${name}/tables`);
      setTableDetails((prev) => ({
        ...prev,
        [name]: data.tables || [],
      }));
      // 加载成功，清除该数据源的错误态
      setTableErrors((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    } catch (e) {
      // 加载失败置错误态（不伪装成空表），由 UI 显示错误+重试
      setTableErrors((prev) => ({
        ...prev,
        [name]: e instanceof Error ? e.message : "加载失败",
      }));
    }
  };

  const reloadConfig = async () => {
    try {
      await apiFetch("/api/datasources/reload", { method: "POST" });
      await fetchData();
    } catch (e) {
      console.warn("重载数据源配置失败:", e);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="text-zinc-500">加载中...</div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-white p-6 dark:bg-zinc-950">
      <div className="mx-auto max-w-5xl">
        {/* 标题栏 */}
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-zinc-900 dark:text-zinc-100">
              🔗 数据源管理
            </h1>
            <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
              联邦数据库——管理多个数据源连接，支持跨库查询
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={reloadConfig}
              className="rounded-md border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
            >
              🔄 重新加载
            </button>
            <a
              href="/dashboard"
              className="rounded-md border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
            >
              ← 返回
            </a>
          </div>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
            ❌ {error}
          </div>
        )}

        {/* 数据源卡片 */}
        <div className="space-y-4">
          {datasources.map((ds) => {
            const testResult = testResults[ds.name];
            const tables = tableDetails[ds.name];
            return (
              <div
                key={ds.name}
                className="rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900"
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
                        {ds.name}
                      </h2>
                      {ds.is_default && (
                        <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-950 dark:text-zinc-400">
                          默认
                        </span>
                      )}
                      <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                        {ds.type}
                      </span>
                    </div>
                    <p className="mt-1 font-mono text-sm text-zinc-500 dark:text-zinc-400">
                      {ds.type === "sqlite"
                        ? `📁 ${ds.database}`
                        : `🌐 ${ds.host}/${ds.database}`}
                    </p>
                    <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
                      已注册表数: {ds.table_count}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => testConnection(ds.name)}
                      disabled={testing === ds.name}
                      className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
                    >
                      {testing === ds.name ? "测试中..." : "🔌 测试连接"}
                    </button>
                    <button
                      onClick={() => loadTables(ds.name)}
                      className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
                    >
                      📋 查看表
                    </button>
                  </div>
                </div>

                {/* 测试结果 */}
                {testResult && (
                  <div
                    className={`mt-3 rounded-md p-3 text-sm ${
                      testResult.ok
                        ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400"
                        : "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400"
                    }`}
                  >
                    {testResult.ok ? "✅" : "❌"} {testResult.message}
                  </div>
                )}

                {/* 表列表加载错误（失败显式显示+重试，不伪装空表） */}
                {tableErrors[ds.name] && (
                  <div className="mt-3 flex items-center justify-between rounded-md bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-400">
                    <span>❌ 表列表加载失败: {tableErrors[ds.name]}</span>
                    <button
                      onClick={() => loadTables(ds.name)}
                      className="rounded border border-red-300 px-2 py-0.5 text-xs font-medium hover:bg-red-100 dark:border-red-800 dark:hover:bg-red-900"
                    >
                      重试
                    </button>
                  </div>
                )}

                {/* 表列表 */}
                {tables && (
                  <div className="mt-3 border-t border-zinc-200 pt-3 dark:border-zinc-700">
                    <p className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
                      表列表 ({tables.length})
                    </p>
                    <div className="max-h-48 space-y-1 overflow-y-auto">
                      {tables.map((t) => (
                        <div
                          key={t.name}
                          className="flex items-center justify-between rounded px-2 py-1 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800"
                        >
                          <span className="font-mono text-zinc-700 dark:text-zinc-300">
                            {t.name}
                          </span>
                          <span className="text-zinc-500 dark:text-zinc-400">
                            {t.rows >= 0 ? `${t.rows} 行` : "—"}
                          </span>
                        </div>
                      ))}
                      {tables.length === 0 && (
                        <div className="py-2 text-center text-sm text-zinc-400">
                          无表
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {datasources.length === 0 && (
            <div className="rounded-[16px] border border-[#ececec] bg-white p-8 text-center text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900">
              暂无数据源配置
            </div>
          )}
        </div>

        {/* 配置提示 */}
        <div className="mt-6 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-700 dark:border-blue-800 dark:bg-blue-950 dark:text-zinc-400">
          <p className="font-medium">💡 配置说明</p>
          <p className="mt-1">
            数据源配置文件位于{" "}
            <code className="rounded bg-blue-100 px-1 dark:bg-blue-900">
              config/datasources.yml
            </code>
          </p>
          <p className="mt-1">修改后点击"重新加载"按钮热重载，无需重启后端</p>
          <p className="mt-1">
            在表的 Schema YAML 中添加{" "}
            <code className="rounded bg-blue-100 px-1 dark:bg-blue-900">
              datasource: 数据源名
            </code>{" "}
            字段可指定表所属数据源
          </p>
        </div>
      </div>
    </div>
  );
}
