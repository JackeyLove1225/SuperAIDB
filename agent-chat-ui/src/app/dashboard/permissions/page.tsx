"use client";

import { apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect, useCallback, useMemo } from "react";

// ── 待审批（审批交互唯一实现：hook + 卡片组件，全局 watcher 同源复用）──
import { useApprovals } from "@/hooks/useApprovals";
import { PendingCard } from "@/components/approvals/pending-card";

// ── 类型与映射逻辑（src/lib/perm-rules.ts，零 React 依赖，有单测锁定）──
import {
  OPS,
  type Op,
  type RulesDoc,
  scopeDenied,
  pruneShells,
  colDenied,
  withDsDenied,
  withTableDenied,
  withColDeny,
  withUserDenied,
  withUserTableDenied,
  withUserColDeny,
  userColDenied,
} from "@/lib/perm-rules";

const OP_LABEL: Record<Op, string> = {
  query: "查询",
  insert: "插入",
  update: "修改",
  delete: "删除",
  ddl: "建改结构",
  drop: "删除结构",
};

interface DsInfo {
  name: string;
  type: string;
  is_default: boolean;
  tables: string[];
  columns: Record<string, string[]>;
}

// 六项操作的"禁止"复选框行（数据源/表级页签共用，与列级同风格）
// 级联语义：parentDenied 中的操作＝上级已禁，渲染为勾选+置灰禁用（不写本级规则，
// 禁止框不可点就不会进 state）；上级解禁后该派生置灰自动消失，恢复可编辑
function DenyBoxes({
  denied,
  onChange,
  parentDenied,
  parentHint,
  scopeHint,
}: {
  denied: Set<Op>;
  onChange: (s: Set<Op>) => void;
  /** 上级（数据源级）已禁的操作集——置灰锁定展示 */
  parentDenied?: Set<Op>;
  /** 锁定提示文案，如"数据源级已禁" */
  parentHint?: string;
  /** 本级作用对象提示，如"整库"/"本表"——六项同名不同域，防混 */
  scopeHint?: string;
}) {
  return (
    <div className="flex flex-wrap gap-5">
      {OPS.map((o) => {
        const locked = parentDenied?.has(o) ?? false;
        return (
          <label
            key={o}
            className={`flex items-center gap-1 text-sm ${
              locked
                ? "cursor-not-allowed text-gray-400 opacity-50"
                : "text-gray-700"
            }`}
          >
            <input
              type="checkbox"
              className="accent-gray-900"
              checked={locked || denied.has(o)}
              disabled={locked}
              onChange={(e) => {
                const s = new Set(denied);
                if (e.target.checked) s.add(o);
                else s.delete(o);
                onChange(s);
              }}
            />
            禁止{OP_LABEL[o]}
            {scopeHint && (
              <span className="text-xs text-gray-400">（{scopeHint}）</span>
            )}
            {locked && parentHint && (
              <span className="text-xs text-gray-400">（{parentHint}）</span>
            )}
          </label>
        );
      })}
    </div>
  );
}

export default function PermissionsPage() {
  const [rules, setRules] = useState<RulesDoc>({});
  const [dsInfos, setDsInfos] = useState<DsInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [tab, setTab] = useState<"ds" | "table" | "column" | "user">("ds");
  const [selDs, setSelDs] = useState("");
  const [selTable, setSelTable] = useState("");
  const [test, setTest] = useState<{
    ds: string;
    table: string;
    op: Op;
    role: string;
  }>({
    ds: "",
    table: "",
    op: "insert",
    role: "",
  });
  const [testResult, setTestResult] = useState<string | null>(null);
  // ── 待审批（高危人审闸：MCP 通道挂起的高危操作在此由 admin 批准/拒绝；
  // 交互逻辑在 useApprovals hook（全局 ApprovalWatcher 同源复用）；
  // askPassword 供"保存规则"等页面自身高危操作复用）──
  const { approvals, settle, askPassword, operatorPasswordModal } =
    useApprovals();

  const load = useCallback(async () => {
    try {
      const data = await apiFetch<{ rules: RulesDoc; datasources: DsInfo[] }>(
        "/api/permissions",
      );
      setRules(data.rules || {});
      setDsInfos(data.datasources || []);
      if (!selDs && data.datasources?.length)
        setSelDs(data.datasources[0].name);
      if (!test.ds && data.datasources?.length)
        setTest((t) => ({ ...t, ds: data.datasources[0].name }));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = async () => {
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setSaving(true);
    setMsg(null);
    try {
      const pruned = pruneShells(rules);
      const r = await apiFetch<{ message?: string }>("/api/permissions", {
        method: "PUT",
        body: JSON.stringify({
          rules: pruned,
          operator_password: operatorPassword,
        }),
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
      const r = await apiFetch<{
        allowed: boolean;
        scope: string;
        mode: string;
        reason: string;
      }>("/api/permissions/test", {
        method: "POST",
        body: JSON.stringify({
          datasource: test.ds,
          table: test.table,
          op: test.op,
          role: test.role,
        }),
      });
      setTestResult(
        r.allowed
          ? `✅ 允许 ${OP_LABEL[test.op]}（${r.scope}规则，${r.mode} 模式）`
          : `⛔ ${r.reason}`,
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
  const setColDeny = (
    ds: string,
    table: string,
    col: string,
    op: Op,
    denied: boolean,
  ) => setRules((r) => withColDeny(r, ds, table, col, op, denied));
  const colDeniedOf = (
    ds: string,
    table: string,
    col: string,
    op: Op,
  ): boolean => colDenied(rules, ds, table, col, op);

  // ── 列级规则：列名模糊筛选 + 批量勾选 ──
  // 筛选只影响显示，不影响已选状态；批量操作只作用于当前筛选结果中的列
  const [colFilter, setColFilter] = useState("");

  const TABS = [
    { k: "ds", label: "数据源规则" },
    { k: "table", label: "表级规则" },
    { k: "column", label: "列级规则" },
    { k: "user", label: "用户规则" },
  ] as const;

  const curDs = dsInfos.find((d) => d.name === selDs);
  const tablesOfDs = curDs?.tables || [];

  // 列级规则的可见列（搜索词模糊过滤列名；接口只回传列名，无业务名字段）
  const allCols = curDs?.columns?.[selTable] || [];
  const kw = colFilter.trim().toLowerCase();
  const filteredCols = kw
    ? allCols.filter((c) => c.toLowerCase().includes(kw))
    : allCols;
  // 列级的级联上级：数据源级 ∪ 表级已禁操作（有效禁止的派生来源，渲染置灰用）
  const dsDeniedSet = scopeDenied(rules.datasources?.[selDs]);
  const tblDeniedSet = scopeDenied(
    rules.datasources?.[selDs]?.tables?.[selTable],
  );
  // 批量勾选：对筛选结果中的每列×每操作设置 deny（逐个走 setRules 函数更新，天然与既有勾选合并）
  const batchSetCols = (
    cols: string[],
    value: (col: string, op: Op) => boolean,
  ) =>
    cols.forEach((col) =>
      OPS.forEach((o) => setColDeny(selDs, selTable, col, o, value(col, o))),
    );

  useEffect(() => {
    if (tablesOfDs.length && !tablesOfDs.includes(selTable))
      setSelTable(tablesOfDs[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selDs, dsInfos]);

  if (loading) return <div className="p-8 text-gray-400">加载权限规则…</div>;

  return (
    <div className="mx-auto h-full max-w-5xl space-y-5 overflow-y-auto p-6">
      {/* 顶栏 */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-900">权限管理</h1>
        <div className="flex items-center gap-3">
          <label className="text-sm text-gray-500">全局默认</label>
          <select
            className="rounded-lg border border-gray-200 px-2 py-1.5 text-sm"
            value={rules.default || "full"}
            onChange={(e) =>
              setRules((r) => ({ ...r, default: e.target.value }))
            }
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
      {msg && (
        <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
          {msg}
        </div>
      )}

      {/* 规则测试 */}
      <div className="space-y-3 rounded-xl border border-gray-200 p-4">
        <div className="text-sm font-medium text-gray-900">
          规则测试（干跑一条判定）
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <select
            className="rounded-lg border border-gray-200 px-2 py-1.5"
            value={test.ds}
            onChange={(e) => setTest((t) => ({ ...t, ds: e.target.value }))}
          >
            {dsInfos.map((d) => (
              <option
                key={d.name}
                value={d.name}
              >
                {d.name}
              </option>
            ))}
          </select>
          <select
            className="rounded-lg border border-gray-200 px-2 py-1.5"
            value={test.table}
            onChange={(e) => setTest((t) => ({ ...t, table: e.target.value }))}
          >
            <option value="">（库级）</option>
            {(dsInfos.find((d) => d.name === test.ds)?.tables || []).map(
              (t) => (
                <option
                  key={t}
                  value={t}
                >
                  {t}
                </option>
              ),
            )}
          </select>
          <select
            className="rounded-lg border border-gray-200 px-2 py-1.5"
            value={test.op}
            onChange={(e) =>
              setTest((t) => ({ ...t, op: e.target.value as Op }))
            }
          >
            {OPS.map((o) => (
              <option
                key={o}
                value={o}
              >
                {OP_LABEL[o]}
              </option>
            ))}
          </select>
          <input
            className="w-28 rounded-lg border border-gray-200 px-2 py-1.5"
            placeholder="角色(可选)"
            value={test.role}
            onChange={(e) => setTest((t) => ({ ...t, role: e.target.value }))}
          />
          <button
            onClick={runTest}
            className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white hover:bg-gray-700"
          >
            测试
          </button>
        </div>
        {testResult && (
          <div className="text-sm text-gray-700">{testResult}</div>
        )}
      </div>

      {/* 待审批（高危人审闸结算区；卡片与全局 watcher 同源 PendingCard） */}
      {approvals.length > 0 && (
        <div className="space-y-3 rounded-xl border border-amber-200 bg-amber-50 p-4">
          <div className="text-sm font-medium text-gray-900">
            ⏸️ 待审批（{approvals.length}）——高危操作需你批准后才会执行
          </div>
          {approvals.map((p) => (
            <PendingCard
              key={p.token}
              p={p}
              onSettle={async (token, approve, name) => {
                const r = await settle(token, approve, name);
                if (r === null) return; // 用户取消密码输入
                setMsg(
                  r.ok
                    ? `✓ ${r.message}${r.result ? `：${r.result.slice(0, 200)}` : ""}`
                    : `✗ ${r.message}`,
                );
              }}
            />
          ))}
        </div>
      )}

      {/* 页签 */}
      <div className="flex gap-1 border-b border-gray-200">
        {TABS.map((t) => (
          <button
            key={t.k}
            onClick={() => setTab(t.k)}
            className={`rounded-t-lg px-4 py-2 text-sm ${tab === t.k ? "bg-gray-900 text-white" : "text-gray-500 hover:bg-gray-100"}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* 数据源规则 */}
      {tab === "ds" && (
        <div className="space-y-3">
          <p className="text-xs text-gray-400">
            本级禁令作用于<b>整个数据库</b>
            ：库内所有表、所有字段、所有记录。前四项（查询/插入/修改/删除）管记录，后两项（建改结构/删除结构）管表和字段的定义。勾选＝禁止该操作；全部不勾＝跟随全局默认。
          </p>
          {dsInfos.map((ds) => (
            <div
              key={ds.name}
              className="rounded-xl border border-gray-200 p-4"
            >
              <div className="mb-3 flex items-center justify-between">
                <div className="font-medium text-gray-900">
                  {ds.name}
                  <span className="ml-2 text-xs text-gray-400">
                    {ds.type}
                    {ds.is_default ? " · 默认" : ""}
                  </span>
                </div>
                <div className="text-xs text-gray-400">
                  模式: {rules.datasources?.[ds.name]?.mode || "跟随全局"}
                </div>
              </div>
              <DenyBoxes
                denied={scopeDenied(rules.datasources?.[ds.name])}
                scopeHint="整库"
                onChange={(s) => setDsDenied(ds.name, s)}
              />
            </div>
          ))}
        </div>
      )}

      {/* 表级规则 */}
      {tab === "table" && (
        <div className="space-y-3">
          <p className="text-xs text-gray-400">
            本级禁令作用于<b>当前选中的一张表</b>
            ：它所有字段、所有记录（整表禁查＝这张表对该角色彻底不可见）；新加的列自动继承禁止。前四项管记录，后两项管这张表的结构。
          </p>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">数据源</span>
            <select
              className="rounded-lg border border-gray-200 px-2 py-1.5"
              value={selDs}
              onChange={(e) => setSelDs(e.target.value)}
            >
              {dsInfos.map((d) => (
                <option
                  key={d.name}
                  value={d.name}
                >
                  {d.name}
                </option>
              ))}
            </select>
            <span className="text-gray-500">表</span>
            <select
              className="rounded-lg border border-gray-200 px-2 py-1.5"
              value={selTable}
              onChange={(e) => setSelTable(e.target.value)}
            >
              {tablesOfDs.map((t) => (
                <option
                  key={t}
                  value={t}
                >
                  {t}
                </option>
              ))}
            </select>
          </div>
          {selTable && (
            <div className="rounded-xl border border-gray-200 p-4">
              <div className="mb-3 flex items-center justify-between">
                <div className="font-medium text-gray-900">{selTable}</div>
                <div className="text-xs text-gray-400">
                  模式:{" "}
                  {rules.datasources?.[selDs]?.tables?.[selTable]?.mode ||
                    "跟随库级"}
                </div>
              </div>
              <DenyBoxes
                denied={scopeDenied(
                  rules.datasources?.[selDs]?.tables?.[selTable],
                )}
                parentDenied={scopeDenied(rules.datasources?.[selDs])}
                parentHint="数据源级已禁"
                scopeHint="本表"
                onChange={(s) => setTableDenied(selDs, selTable, s)}
              />
              <p className="mt-2 text-xs text-gray-400">
                勾选＝禁止该操作；全部不勾＝跟随库级规则
              </p>
            </div>
          )}
        </div>
      )}

      {/* 列级规则 */}
      {tab === "column" && (
        <div className="space-y-3">
          <p className="text-xs text-gray-400">
            本级禁令只作用于<b>选中的字段</b>
            ：禁查＝查询结果里该字段被屏蔽；禁插入/修改＝不能给该字段赋值；禁删除＝不能拿该字段当删除/更新条件；结构两项＝不能改/删该字段的定义。日后新加的列默认放行，要禁需回来补勾。
          </p>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">数据源</span>
            <select
              className="rounded-lg border border-gray-200 px-2 py-1.5"
              value={selDs}
              onChange={(e) => setSelDs(e.target.value)}
            >
              {dsInfos.map((d) => (
                <option
                  key={d.name}
                  value={d.name}
                >
                  {d.name}
                </option>
              ))}
            </select>
            <span className="text-gray-500">表</span>
            <select
              className="rounded-lg border border-gray-200 px-2 py-1.5"
              value={selTable}
              onChange={(e) => setSelTable(e.target.value)}
            >
              {tablesOfDs.map((t) => (
                <option
                  key={t}
                  value={t}
                >
                  {t}
                </option>
              ))}
            </select>
          </div>
          {selTable && (
            <>
              {/* 列筛选 + 批量勾选（作用于筛选结果；不影响已选状态） */}
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <input
                  className="rounded-lg border border-gray-200 px-2 py-1.5"
                  placeholder="按列名筛选…"
                  value={colFilter}
                  onChange={(e) => setColFilter(e.target.value)}
                />
                <button
                  onClick={() => batchSetCols(filteredCols, () => true)}
                  disabled={filteredCols.length === 0}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                >
                  全选
                </button>
                <button
                  onClick={() =>
                    batchSetCols(
                      filteredCols,
                      (col, o) => !colDeniedOf(selDs, selTable, col, o),
                    )
                  }
                  disabled={filteredCols.length === 0}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                >
                  反选
                </button>
                <button
                  onClick={() => batchSetCols(filteredCols, () => false)}
                  disabled={filteredCols.length === 0}
                  className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                >
                  清空
                </button>
                {kw && (
                  <span className="text-xs text-gray-400">
                    匹配 {filteredCols.length} / {allCols.length} 列
                  </span>
                )}
              </div>
              <div className="overflow-hidden rounded-xl border border-gray-200">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 text-xs text-gray-500">
                    <tr>
                      <th className="px-4 py-2 text-left font-medium">列</th>
                      {OPS.map((o) => (
                        <th
                          key={o}
                          className="px-2 py-2 text-center font-medium"
                        >
                          禁止{OP_LABEL[o]}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredCols.map((col) => (
                      <tr
                        key={col}
                        className="border-t border-gray-100"
                      >
                        <td className="px-4 py-2 text-gray-900">{col}</td>
                        {OPS.map((o) => {
                          // 级联：上级（数据源级/表级）已禁 → 勾选+置灰禁用，不写列级规则
                          const lockedBy = dsDeniedSet.has(o)
                            ? "数据源级已禁"
                            : tblDeniedSet.has(o)
                              ? "表级已禁"
                              : null;
                          return (
                            <td
                              key={o}
                              className="px-2 py-2 text-center"
                            >
                              <input
                                type="checkbox"
                                className={`accent-gray-900 ${lockedBy ? "cursor-not-allowed opacity-50" : ""}`}
                                checked={
                                  lockedBy !== null ||
                                  colDeniedOf(selDs, selTable, col, o)
                                }
                                disabled={lockedBy !== null}
                                onChange={(e) =>
                                  setColDeny(
                                    selDs,
                                    selTable,
                                    col,
                                    o,
                                    e.target.checked,
                                  )
                                }
                              />
                              {lockedBy && (
                                <div className="text-xs text-gray-400">
                                  {lockedBy}
                                </div>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {/* 用户规则（按具体用户授权，级联在 数据源→表 之后，只严不松） */}
      {tab === "user" && (
        <UserTab
          rules={rules}
          setRules={setRules}
          dsInfos={dsInfos}
        />
      )}

      {/* 操作密码确认弹窗（高危操作第二因子） */}
      {operatorPasswordModal}
    </div>
  );
}

/** 用户规则页签：按具体用户授权（rules.users.<用户名>）。
 * 结构与 datasources 同构（操作级 deny/allow + tables.<t> + columns.<c>），
 * 但表不挂数据源下——用户规则跨数据源对同名表生效；
 * 级联在 数据源→表 之后，只能更严（上级已禁的在此解禁无效）。
 * 保存复用页面顶部"保存并生效"（PUT /api/permissions，带 operator_password）。 */
function UserTab({
  rules,
  setRules,
  dsInfos,
}: {
  rules: RulesDoc;
  setRules: React.Dispatch<React.SetStateAction<RulesDoc>>;
  dsInfos: DsInfo[];
}) {
  const [apiUsers, setApiUsers] = useState<string[]>([]);
  const [selUser, setSelUser] = useState("");
  const [newUser, setNewUser] = useState("");
  const [selTable, setSelTable] = useState("");
  const [colFilter, setColFilter] = useState("");

  // 用户名列表：GET /api/auth/users（admin 端点；非 admin 静默失败，回退 rules.users 已有键）
  useEffect(() => {
    apiFetch<{ users: { username: string }[] }>("/api/auth/users")
      .then((d) => setApiUsers((d.users || []).map((u) => u.username)))
      .catch(() => {});
  }, []);

  const userNames = useMemo(() => {
    const s = new Set([...apiUsers, ...Object.keys(rules.users || {})]);
    return [...s];
  }, [apiUsers, rules.users]);

  useEffect(() => {
    if (!selUser && userNames.length) setSelUser(userNames[0]);
  }, [userNames, selUser]);

  const userScope = rules.users?.[selUser];

  // 表选项：全部数据源的表 ∪ 该用户已有表规则键
  const allTables = useMemo(() => {
    const s = new Set<string>();
    dsInfos.forEach((d) => d.tables.forEach((t) => s.add(t)));
    Object.keys(userScope?.tables || {}).forEach((t) => s.add(t));
    return [...s].sort();
  }, [dsInfos, userScope]);

  useEffect(() => {
    if (allTables.length && !allTables.includes(selTable))
      setSelTable(allTables[0]);
  }, [allTables, selTable]);

  // 列选项：该表在各数据源的列名并集
  const allCols = useMemo(() => {
    const s = new Set<string>();
    dsInfos.forEach((d) =>
      (d.columns[selTable] || []).forEach((c) => s.add(c)),
    );
    return [...s];
  }, [dsInfos, selTable]);
  const kw = colFilter.trim().toLowerCase();
  const filteredCols = kw
    ? allCols.filter((c) => c.toLowerCase().includes(kw))
    : allCols;

  const userColDeniedOf = (col: string, op: Op) =>
    userColDenied(rules, selUser, selTable, col, op);
  // 批量勾选：只作用于筛选结果中的列；筛选不影响已选状态
  const batchSetCols = (
    cols: string[],
    value: (col: string, op: Op) => boolean,
  ) =>
    cols.forEach((col) =>
      OPS.forEach((o) =>
        setRules((r) =>
          withUserColDeny(r, selUser, selTable, col, o, value(col, o)),
        ),
      ),
    );

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-400">
        本级给指定用户个人加限制（users.&lt;用户名&gt;），与角色无关；上级（数据源/表）已禁的用户无法在此解禁。
      </p>

      {/* 用户选择器 */}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-gray-500">用户</span>
        <select
          className="rounded-lg border border-gray-200 px-2 py-1.5"
          value={selUser}
          onChange={(e) => setSelUser(e.target.value)}
        >
          {userNames.length === 0 && <option value="">（无已知用户）</option>}
          {userNames.map((u) => (
            <option
              key={u}
              value={u}
            >
              {u}
            </option>
          ))}
        </select>
        <input
          className="w-36 rounded-lg border border-gray-200 px-2 py-1.5"
          placeholder="新用户名"
          value={newUser}
          onChange={(e) => setNewUser(e.target.value)}
        />
        <button
          className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
          onClick={() => {
            const u = newUser.trim();
            if (u) {
              setSelUser(u);
              setNewUser("");
            }
          }}
        >
          添加
        </button>
        <span className="text-xs text-gray-400">
          用户名列表来自用户管理；也可直接输入 rules.users 中已有的键
        </span>
      </div>

      {selUser && (
        <>
          {/* 操作级（对该用户一切数据源生效） */}
          <div className="rounded-xl border border-gray-200 p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="font-medium text-gray-900">{selUser}</div>
              <div className="text-xs text-gray-400">
                模式: {rules.users?.[selUser]?.mode || "跟随上级"}
              </div>
            </div>
            <DenyBoxes
              denied={scopeDenied(rules.users?.[selUser])}
              onChange={(s) => setRules((r) => withUserDenied(r, selUser, s))}
            />
            <p className="mt-2 text-xs text-gray-400">
              勾选＝禁止该操作（对该用户的一切数据源生效）；全部不勾＝跟随上级规则
            </p>
          </div>

          {/* 用户级表规则 */}
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-500">表</span>
              <select
                className="rounded-lg border border-gray-200 px-2 py-1.5"
                value={selTable}
                onChange={(e) => setSelTable(e.target.value)}
              >
                {allTables.map((t) => (
                  <option
                    key={t}
                    value={t}
                  >
                    {t}
                  </option>
                ))}
              </select>
            </div>
            {selTable && (
              <div className="rounded-xl border border-gray-200 p-4">
                <div className="mb-3 flex items-center justify-between">
                  <div className="font-medium text-gray-900">{selTable}</div>
                  <div className="text-xs text-gray-400">
                    模式:{" "}
                    {rules.users?.[selUser]?.tables?.[selTable]?.mode ||
                      "跟随用户级"}
                  </div>
                </div>
                <DenyBoxes
                  denied={scopeDenied(
                    rules.users?.[selUser]?.tables?.[selTable],
                  )}
                  onChange={(s) =>
                    setRules((r) =>
                      withUserTableDenied(r, selUser, selTable, s),
                    )
                  }
                />
                <p className="mt-2 text-xs text-gray-400">
                  勾选＝禁止该用户操作此表；全部不勾＝跟随用户级规则
                </p>
              </div>
            )}

            {/* 用户级列规则（筛选 + 批量勾选，与列级页签同形态） */}
            {selTable && (
              <>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <input
                    className="rounded-lg border border-gray-200 px-2 py-1.5"
                    placeholder="按列名筛选…"
                    value={colFilter}
                    onChange={(e) => setColFilter(e.target.value)}
                  />
                  <button
                    onClick={() => batchSetCols(filteredCols, () => true)}
                    disabled={filteredCols.length === 0}
                    className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                  >
                    全选
                  </button>
                  <button
                    onClick={() =>
                      batchSetCols(
                        filteredCols,
                        (col, o) => !userColDeniedOf(col, o),
                      )
                    }
                    disabled={filteredCols.length === 0}
                    className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                  >
                    反选
                  </button>
                  <button
                    onClick={() => batchSetCols(filteredCols, () => false)}
                    disabled={filteredCols.length === 0}
                    className="rounded-lg border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                  >
                    清空
                  </button>
                  {kw && (
                    <span className="text-xs text-gray-400">
                      匹配 {filteredCols.length} / {allCols.length} 列
                    </span>
                  )}
                </div>
                <div className="overflow-hidden rounded-xl border border-gray-200">
                  <table className="w-full text-sm">
                    <thead className="bg-gray-50 text-xs text-gray-500">
                      <tr>
                        <th className="px-4 py-2 text-left font-medium">列</th>
                        {OPS.map((o) => (
                          <th
                            key={o}
                            className="px-2 py-2 text-center font-medium"
                          >
                            禁止{OP_LABEL[o]}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {filteredCols.map((col) => (
                        <tr
                          key={col}
                          className="border-t border-gray-100"
                        >
                          <td className="px-4 py-2 text-gray-900">{col}</td>
                          {OPS.map((o) => (
                            <td
                              key={o}
                              className="px-2 py-2 text-center"
                            >
                              <input
                                type="checkbox"
                                className="accent-gray-900"
                                checked={userColDeniedOf(col, o)}
                                onChange={(e) =>
                                  setRules((r) =>
                                    withUserColDeny(
                                      r,
                                      selUser,
                                      selTable,
                                      col,
                                      o,
                                      e.target.checked,
                                    ),
                                  )
                                }
                              />
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
