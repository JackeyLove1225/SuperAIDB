"use client";

import { apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect, useCallback } from "react";

// ── 类型与映射逻辑（src/lib/perm-rules.ts，零 React 依赖，有单测锁定）──
import {
  OPS, type Op, type RulesDoc,
  scopeDenied, pruneShells, colDenied,
  withDsDenied, withTableDenied, withColDeny, withRoleRules,
} from "@/lib/perm-rules";

const OP_LABEL: Record<Op, string> = {
  query: "查询", insert: "插入", update: "修改", delete: "删除", ddl: "建改结构", drop: "删除结构",
};

interface DsInfo {
  name: string; type: string; is_default: boolean;
  tables: string[]; columns: Record<string, string[]>;
}

// 六项操作的"禁止"复选框行（数据源/表级页签共用，与列级同风格）
function DenyBoxes({ denied, onChange }: { denied: Set<Op>; onChange: (s: Set<Op>) => void }) {
  return (
    <div className="flex flex-wrap gap-5">
      {OPS.map((o) => (
        <label key={o} className="flex items-center gap-1 text-sm text-gray-700">
          <input type="checkbox" className="accent-gray-900"
            checked={denied.has(o)}
            onChange={(e) => {
              const s = new Set(denied);
              if (e.target.checked) s.add(o); else s.delete(o);
              onChange(s);
            }} />
          禁止{OP_LABEL[o]}
        </label>
      ))}
    </div>
  );
}

export default function PermissionsPage() {
  const [rules, setRules] = useState<RulesDoc>({});
  const [dsInfos, setDsInfos] = useState<DsInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [tab, setTab] = useState<"ds" | "table" | "column" | "role">("ds");
  const [selDs, setSelDs] = useState("");
  const [selTable, setSelTable] = useState("");
  const [test, setTest] = useState<{ ds: string; table: string; op: Op; role: string }>({
    ds: "", table: "", op: "insert", role: "",
  });
  const [testResult, setTestResult] = useState<string | null>(null);

  // ── 待审批（高危人审闸：MCP 通道挂起的高危操作在此由 admin 批准/拒绝）──
  interface PendingItem { token: string; name: string; impact: string; ttl_remaining: number; age_seconds: number }
  const [approvals, setApprovals] = useState<PendingItem[]>([]);
  const loadApprovals = useCallback(async () => {
    try {
      const d = await apiFetch<{ pending: PendingItem[] }>("/api/approvals");
      setApprovals(d.pending || []);
    } catch { /* 非 admin 或服务未达时静默 */ }
  }, []);
  useEffect(() => {
    loadApprovals();
    const t = setInterval(loadApprovals, 5000);  // 5s 轮询：挂起随时可能来
    return () => clearInterval(t);
  }, [loadApprovals]);
  const settle = async (token: string, approve: boolean) => {
    try {
      const r = await apiFetch<{ message?: string; result?: string }>(
        `/api/approvals/${token}/settle`,
        { method: "POST", body: JSON.stringify({ approve }) }
      );
      setMsg(`✓ ${r.message || "已结算"}${r.result ? `：${String(r.result).slice(0, 200)}` : ""}`);
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : "结算失败"}`);
    }
    loadApprovals();
  };

  const load = useCallback(async () => {
    try {
      const data = await apiFetch<{ rules: RulesDoc; datasources: DsInfo[] }>("/api/permissions");
      setRules(data.rules || {});
      setDsInfos(data.datasources || []);
      if (!selDs && data.datasources?.length) setSelDs(data.datasources[0].name);
      if (!test.ds && data.datasources?.length)
        setTest((t) => ({ ...t, ds: data.datasources[0].name }));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const pruned = pruneShells(rules);
      const r = await apiFetch<{ message?: string }>("/api/permissions", {
        method: "PUT",
        body: JSON.stringify({ rules: pruned }),
      });
      setRules(pruned);
      setMsg(`✓ ${r.message || "已保存并生效"}`);
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : "保存失败"}`);
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTestResult(null);
    try {
      const r = await apiFetch<{ allowed: boolean; scope: string; mode: string; reason: string }>(
        "/api/permissions/test",
        { method: "POST", body: JSON.stringify({ datasource: test.ds, table: test.table, op: test.op, role: test.role }) }
      );
      setTestResult(
        r.allowed
          ? `✅ 允许 ${OP_LABEL[test.op]}（${r.scope}规则，${r.mode} 模式）`
          : `⛔ ${r.reason}`
      );
    } catch (e) {
      setTestResult(`✗ ${e instanceof Error ? e.message : "测试失败"}`);
    }
  };

  // ── 规则编辑助手（薄封装：映射逻辑在 src/lib/perm-rules.ts，有单测）──
  const setDsDenied = (ds: string, denied: Set<Op>) =>
    setRules((r) => withDsDenied(r, ds, denied));
  const setTableDenied = (ds: string, table: string, denied: Set<Op>) =>
    setRules((r) => withTableDenied(r, ds, table, denied));
  const setColDeny = (ds: string, table: string, col: string, op: Op, denied: boolean) =>
    setRules((r) => withColDeny(r, ds, table, col, op, denied));
  const colDeniedOf = (ds: string, table: string, col: string, op: Op): boolean =>
    colDenied(rules, ds, table, col, op);

  const TABS = [
    { k: "ds", label: "数据源规则" },
    { k: "table", label: "表级规则" },
    { k: "column", label: "列级规则" },
    { k: "role", label: "角色" },
  ] as const;

  const curDs = dsInfos.find((d) => d.name === selDs);
  const tablesOfDs = curDs?.tables || [];

  useEffect(() => {
    if (tablesOfDs.length && !tablesOfDs.includes(selTable)) setSelTable(tablesOfDs[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selDs, dsInfos]);

  if (loading) return <div className="p-8 text-gray-400">加载权限规则…</div>;

  return (
    <div className="mx-auto max-w-5xl p-6 space-y-5">
      {/* 顶栏 */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-900">权限管理</h1>
        <div className="flex items-center gap-3">
          <label className="text-sm text-gray-500">全局默认</label>
          <select
            className="rounded-lg border border-gray-200 px-2 py-1.5 text-sm"
            value={rules.default || "full"}
            onChange={(e) => setRules((r) => ({ ...r, default: e.target.value }))}
          >
            <option value="full">全部允许</option>
            <option value="read_only">默认只读</option>
          </select>
          <button
            onClick={save}
            disabled={saving}
            className="rounded-lg bg-gray-900 px-4 py-1.5 text-sm text-white hover:bg-gray-700 disabled:opacity-50"
          >
            {saving ? "保存中…" : "保存并生效"}
          </button>
        </div>
      </div>
      {msg && <div className="rounded-lg bg-gray-50 border border-gray-200 px-3 py-2 text-sm text-gray-700">{msg}</div>}

      {/* 规则测试 */}
      <div className="rounded-xl border border-gray-200 p-4 space-y-3">
        <div className="text-sm font-medium text-gray-900">规则测试（干跑一条判定）</div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={test.ds}
            onChange={(e) => setTest((t) => ({ ...t, ds: e.target.value }))}>
            {dsInfos.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
          </select>
          <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={test.table}
            onChange={(e) => setTest((t) => ({ ...t, table: e.target.value }))}>
            <option value="">（库级）</option>
            {(dsInfos.find((d) => d.name === test.ds)?.tables || []).map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={test.op}
            onChange={(e) => setTest((t) => ({ ...t, op: e.target.value as Op }))}>
            {OPS.map((o) => <option key={o} value={o}>{OP_LABEL[o]}</option>)}
          </select>
          <input className="rounded-lg border border-gray-200 px-2 py-1.5 w-28" placeholder="角色(可选)"
            value={test.role} onChange={(e) => setTest((t) => ({ ...t, role: e.target.value }))} />
          <button onClick={runTest} className="rounded-lg bg-gray-900 px-3 py-1.5 text-white text-sm hover:bg-gray-700">测试</button>
        </div>
        {testResult && <div className="text-sm text-gray-700">{testResult}</div>}
      </div>

      {/* 待审批（高危人审闸结算区） */}
      {approvals.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 space-y-3">
          <div className="text-sm font-medium text-gray-900">
            ⏸️ 待审批（{approvals.length}）——高危操作需你批准后才会执行
          </div>
          {approvals.map((p) => (
            <div key={p.token} className="rounded-lg border border-amber-200 bg-white p-3 space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-mono text-sm text-gray-900">{p.name}</span>
                <span className="text-xs text-gray-400">剩余 {Math.max(0, p.ttl_remaining)}s</span>
              </div>
              <pre className="max-h-32 overflow-auto whitespace-pre-wrap text-xs text-gray-600">{p.impact}</pre>
              <div className="flex gap-2">
                <button onClick={() => settle(p.token, true)}
                  className="rounded-lg bg-gray-900 px-3 py-1 text-xs text-white hover:bg-gray-700">批准执行</button>
                <button onClick={() => settle(p.token, false)}
                  className="rounded-lg border border-gray-300 px-3 py-1 text-xs text-gray-700 hover:bg-gray-50">拒绝</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 页签 */}
      <div className="flex gap-1 border-b border-gray-200">
        {TABS.map((t) => (
          <button key={t.k} onClick={() => setTab(t.k)}
            className={`px-4 py-2 text-sm rounded-t-lg ${tab === t.k ? "bg-gray-900 text-white" : "text-gray-500 hover:bg-gray-100"}`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* 数据源规则 */}
      {tab === "ds" && (
        <div className="space-y-3">
          <p className="text-xs text-gray-400">勾选＝禁止该操作；全部不勾＝跟随全局默认（要"仅允许某操作"，勾掉其余全部即可）</p>
          {dsInfos.map((ds) => (
            <div key={ds.name} className="rounded-xl border border-gray-200 p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="font-medium text-gray-900">
                  {ds.name}
                  <span className="ml-2 text-xs text-gray-400">{ds.type}{ds.is_default ? " · 默认" : ""}</span>
                </div>
                <div className="text-xs text-gray-400">
                  模式: {rules.datasources?.[ds.name]?.mode || "跟随全局"}
                </div>
              </div>
              <DenyBoxes denied={scopeDenied(rules.datasources?.[ds.name])}
                onChange={(s) => setDsDenied(ds.name, s)} />
            </div>
          ))}
        </div>
      )}

      {/* 表级规则 */}
      {tab === "table" && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">数据源</span>
            <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={selDs}
              onChange={(e) => setSelDs(e.target.value)}>
              {dsInfos.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
            </select>
            <span className="text-gray-500">表</span>
            <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={selTable}
              onChange={(e) => setSelTable(e.target.value)}>
              {tablesOfDs.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          {selTable && (
            <div className="rounded-xl border border-gray-200 p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="font-medium text-gray-900">{selTable}</div>
                <div className="text-xs text-gray-400">
                  模式: {rules.datasources?.[selDs]?.tables?.[selTable]?.mode || "跟随库级"}
                </div>
              </div>
              <DenyBoxes denied={scopeDenied(rules.datasources?.[selDs]?.tables?.[selTable])}
                onChange={(s) => setTableDenied(selDs, selTable, s)} />
              <p className="mt-2 text-xs text-gray-400">勾选＝禁止该操作；全部不勾＝跟随库级规则</p>
            </div>
          )}
        </div>
      )}

      {/* 列级规则 */}
      {tab === "column" && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">数据源</span>
            <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={selDs}
              onChange={(e) => setSelDs(e.target.value)}>
              {dsInfos.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
            </select>
            <span className="text-gray-500">表</span>
            <select className="rounded-lg border border-gray-200 px-2 py-1.5" value={selTable}
              onChange={(e) => setSelTable(e.target.value)}>
              {tablesOfDs.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          {selTable && (
            <div className="rounded-xl border border-gray-200 overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-gray-500 text-xs">
                  <tr>
                    <th className="text-left px-4 py-2 font-medium">列</th>
                    {OPS.map((o) => <th key={o} className="px-2 py-2 font-medium text-center">禁止{OP_LABEL[o]}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {(curDs?.columns?.[selTable] || []).map((col) => (
                    <tr key={col} className="border-t border-gray-100">
                      <td className="px-4 py-2 text-gray-900">{col}</td>
                      {OPS.map((o) => (
                        <td key={o} className="px-2 py-2 text-center">
                          <input type="checkbox" className="accent-gray-900"
                            checked={colDeniedOf(selDs, selTable, col, o)}
                            onChange={(e) => setColDeny(selDs, selTable, col, o, e.target.checked)} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* 角色 */}
      {tab === "role" && <RoleTab rules={rules} setRules={setRules} />}
    </div>
  );
}

function RoleTab({ rules, setRules }: { rules: RulesDoc; setRules: React.Dispatch<React.SetStateAction<RulesDoc>> }) {
  const [newRole, setNewRole] = useState("");
  const roles = rules.roles || {};

  const setRoleRules = (name: string, patch: Partial<{ allow: Op[]; deny: Op[] }>) =>
    setRules((r) => withRoleRules(r, name, patch));

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm">
        <input className="rounded-lg border border-gray-200 px-2 py-1.5" placeholder="新角色名（如 readonly）"
          value={newRole} onChange={(e) => setNewRole(e.target.value)} />
        <button
          className="rounded-lg bg-gray-900 px-3 py-1.5 text-white text-sm hover:bg-gray-700"
          onClick={() => { if (newRole.trim()) { setRoleRules(newRole.trim(), { deny: [] }); setNewRole(""); } }}>
          添加角色
        </button>
        <span className="text-xs text-gray-400">引擎已就绪；用户体系后续接入（当前经 API 注入角色名生效）</span>
      </div>
      {Object.keys(roles).length === 0 && <div className="text-sm text-gray-400">暂无角色</div>}
      {Object.entries(roles).map(([name, rr]) => {
        const mode = rr.allow ? "allow" : "deny";
        const list = (mode === "allow" ? rr.allow : rr.deny) || [];
        return (
          <div key={name} className="rounded-xl border border-gray-200 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="font-medium text-gray-900">{name}</div>
              <div className="flex items-center gap-2 text-sm">
                <select className="rounded-lg border border-gray-200 px-2 py-1 text-sm" value={mode}
                  onChange={(e) => setRoleRules(name, e.target.value === "allow" ? { allow: [] } : { deny: [] })}>
                  <option value="deny">黑名单（禁所选）</option>
                  <option value="allow">白名单（仅允许所选）</option>
                </select>
                <button className="text-gray-400 hover:text-gray-700 text-sm"
                  onClick={() => setRules((r) => { const m = { ...(r.roles || {}) }; delete m[name]; return { ...r, roles: m }; })}>
                  删除
                </button>
              </div>
            </div>
            <div className="flex gap-5">
              {OPS.map((o) => (
                <label key={o} className="flex items-center gap-1 text-sm text-gray-700">
                  <input type="checkbox" className="accent-gray-900"
                    checked={list.includes(o)}
                    onChange={(e) => {
                      const s = new Set(list);
                      if (e.target.checked) s.add(o); else s.delete(o);
                      setRoleRules(name, { [mode]: [...s] as Op[] });
                    }} />
                  {OP_LABEL[o]}
                </label>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
