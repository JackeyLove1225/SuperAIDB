// 权限规则 ⇄ UI 复选框 的纯映射逻辑（自 permissions/page.tsx 提取，零 React 依赖，可单测）
//
// 契约约束：本文件产出的 rules JSON 形状即 PUT /api/permissions 的契约，
// 与后端 core/permission/policy.py 的解析语义一一对应：
//   - 库级/表级勾选产物 = { mode: "custom", deny: [...] }（空集=删除本层覆盖，跟随上级）
//   - 列级勾选产物 = tables.<t>.columns.<c>.deny = [...]（空数组=删列节点）
//   - 壳节点（无 mode/allow/deny）不产生语义，后端按"向上继承"处理
// 结构回归由 perm-rules.test.ts 锁定；语义回归由 data-engine 权限矩阵锁定。

export const OPS = [
  "query",
  "insert",
  "update",
  "delete",
  "ddl",
  "drop",
] as const;
export type Op = (typeof OPS)[number];

export interface ScopeRules {
  mode?: string;
  allow?: Op[];
  deny?: Op[];
  tables?: Record<string, TableRules>;
}
export interface TableRules extends ScopeRules {
  columns?: Record<string, { deny?: Op[] }>;
}
export interface RulesDoc {
  default?: string;
  datasources?: Record<string, ScopeRules>;
  // users.<用户名>：与 datasources 节点同构（deny/allow + tables.<t>.{deny, columns}），
  // 但表不挂数据源下——用户规则跨数据源对同名表生效，级联在 数据源→表 之后只严不松
  users?: Record<string, ScopeRules>;
}

// 规则结构 → 禁止集合（复选框显示语义）：
// 无覆盖/full=全放；read_only=禁查询外全部；custom+deny=黑名单位；
// custom+allow=白名单补集；显式 custom 裸壳=fail-closed 全禁；
// 无 mode/allow/deny 的壳=不产生语义（向上继承，与引擎一致——
// 曾把"仅挂 tables 的壳"误当全禁：清空最后一项勾选时全部回勾）
export function scopeDenied(scope?: ScopeRules): Set<Op> {
  if (!scope) return new Set();
  if (scope.mode === "full") return new Set();
  if (scope.mode === "read_only")
    return new Set(OPS.filter((o) => o !== "query"));
  if (scope.allow !== undefined)
    return new Set(OPS.filter((o) => !scope.allow!.includes(o)));
  if (scope.deny !== undefined) return new Set(scope.deny);
  if (scope.mode === "custom") return new Set(OPS);
  return new Set();
}

// 保存前摘除空壳：未声明 mode/allow/deny 且无有效子节点的 scope 不下发，
// 保持文件整洁（策略层对壳按"向上继承"处理，此项仅为文件卫生，不改变语义）
export function pruneShells(rules: RulesDoc): RulesDoc {
  const hasScope = (s: ScopeRules) =>
    s.mode !== undefined || s.allow !== undefined || s.deny !== undefined;
  // datasources/users 同构（scope → tables → columns 三层），共用一份摘壳逻辑
  const pruneScopeMap = (map?: Record<string, ScopeRules>) => {
    const outMap: Record<string, ScopeRules> = {};
    for (const [key, scope] of Object.entries(map || {})) {
      const s: ScopeRules = { ...scope };
      if (s.tables) {
        const tMap: NonNullable<ScopeRules["tables"]> = {};
        for (const [t, ts] of Object.entries(s.tables)) {
          const t2: TableRules = { ...ts };
          if (t2.columns) {
            const cMap = Object.fromEntries(
              Object.entries(t2.columns).filter(
                ([, c]) => (c.deny || []).length > 0,
              ),
            );
            if (Object.keys(cMap).length) t2.columns = cMap;
            else delete t2.columns;
          }
          if (hasScope(t2) || t2.columns) tMap[t] = t2;
        }
        if (Object.keys(tMap).length) s.tables = tMap;
        else delete s.tables;
      }
      if (hasScope(s) || s.tables) outMap[key] = s;
    }
    return outMap;
  };
  const out: RulesDoc = { ...rules };
  out.datasources = pruneScopeMap(out.datasources);
  if (out.users) out.users = pruneScopeMap(out.users);
  return out;
}

// ── 勾选 → 规则结构（写方向）──
// 禁止集合 → 规则结构：空集=删除本层覆盖（跟随上级），非空=custom 黑名单。
// 黑名单表达力完备："仅允许 X"=勾掉除 X 外全部。写 scope 前清掉旧
// mode/allow/deny（防扩散合并把历史键带回来）。

export function withDsDenied(
  r: RulesDoc,
  ds: string,
  denied: Set<Op>,
): RulesDoc {
  const dsMap = { ...(r.datasources || {}) };
  const prev = { ...(dsMap[ds] || {}) };
  delete prev.mode;
  delete prev.allow;
  delete prev.deny;
  if (denied.size === 0) {
    if (!prev.tables) delete dsMap[ds];
    else dsMap[ds] = prev;
  } else {
    dsMap[ds] = { ...prev, mode: "custom", deny: [...denied] };
  }
  return { ...r, datasources: dsMap };
}

export function withTableDenied(
  r: RulesDoc,
  ds: string,
  table: string,
  denied: Set<Op>,
): RulesDoc {
  const dsMap = { ...(r.datasources || {}) };
  const dsScope = { ...(dsMap[ds] || {}) };
  const tables = { ...(dsScope.tables || {}) };
  const prev = { ...(tables[table] || {}) };
  delete prev.mode;
  delete prev.allow;
  delete prev.deny;
  if (denied.size === 0) {
    if (!prev.columns) delete tables[table];
    else tables[table] = prev;
  } else {
    tables[table] = { ...prev, mode: "custom", deny: [...denied] };
  }
  dsScope.tables = tables;
  dsMap[ds] = dsScope;
  return { ...r, datasources: dsMap };
}

export function withColDeny(
  r: RulesDoc,
  ds: string,
  table: string,
  col: string,
  op: Op,
  denied: boolean,
): RulesDoc {
  const dsMap = { ...(r.datasources || {}) };
  const dsScope = { ...(dsMap[ds] || {}) };
  const tables = { ...(dsScope.tables || {}) };
  const tScope = { ...(tables[table] || {}) };
  const columns = { ...(tScope.columns || {}) };
  const cRules = { ...(columns[col] || {}) };
  const denySet = new Set(cRules.deny || []);
  if (denied) denySet.add(op);
  else denySet.delete(op);
  cRules.deny = [...denySet] as Op[];
  if (cRules.deny.length === 0) delete cRules.deny;
  if (Object.keys(cRules).length === 0) delete columns[col];
  else columns[col] = cRules;
  tScope.columns = columns;
  tables[table] = tScope;
  dsScope.tables = tables;
  dsMap[ds] = dsScope;
  return { ...r, datasources: dsMap };
}

export function colDenied(
  rules: RulesDoc,
  ds: string,
  table: string,
  col: string,
  op: Op,
): boolean {
  const c = rules.datasources?.[ds]?.tables?.[table]?.columns?.[col];
  return !!c?.deny?.includes(op);
}

// ── users.<用户名> 路径助手（与 datasources 三个写助手同构，仅根键不同）──

export function withUserDenied(
  r: RulesDoc,
  user: string,
  denied: Set<Op>,
): RulesDoc {
  const usersMap = { ...(r.users || {}) };
  const prev = { ...(usersMap[user] || {}) };
  delete prev.mode;
  delete prev.allow;
  delete prev.deny;
  if (denied.size === 0) {
    if (!prev.tables) delete usersMap[user];
    else usersMap[user] = prev;
  } else {
    usersMap[user] = { ...prev, mode: "custom", deny: [...denied] };
  }
  return { ...r, users: usersMap };
}

export function withUserTableDenied(
  r: RulesDoc,
  user: string,
  table: string,
  denied: Set<Op>,
): RulesDoc {
  const usersMap = { ...(r.users || {}) };
  const uScope = { ...(usersMap[user] || {}) };
  const tables = { ...(uScope.tables || {}) };
  const prev = { ...(tables[table] || {}) };
  delete prev.mode;
  delete prev.allow;
  delete prev.deny;
  if (denied.size === 0) {
    if (!prev.columns) delete tables[table];
    else tables[table] = prev;
  } else {
    tables[table] = { ...prev, mode: "custom", deny: [...denied] };
  }
  uScope.tables = tables;
  usersMap[user] = uScope;
  return { ...r, users: usersMap };
}

export function withUserColDeny(
  r: RulesDoc,
  user: string,
  table: string,
  col: string,
  op: Op,
  denied: boolean,
): RulesDoc {
  const usersMap = { ...(r.users || {}) };
  const uScope = { ...(usersMap[user] || {}) };
  const tables = { ...(uScope.tables || {}) };
  const tScope = { ...(tables[table] || {}) };
  const columns = { ...(tScope.columns || {}) };
  const cRules = { ...(columns[col] || {}) };
  const denySet = new Set(cRules.deny || []);
  if (denied) denySet.add(op);
  else denySet.delete(op);
  cRules.deny = [...denySet] as Op[];
  if (cRules.deny.length === 0) delete cRules.deny;
  if (Object.keys(cRules).length === 0) delete columns[col];
  else columns[col] = cRules;
  tScope.columns = columns;
  tables[table] = tScope;
  uScope.tables = tables;
  usersMap[user] = uScope;
  return { ...r, users: usersMap };
}

export function userColDenied(
  rules: RulesDoc,
  user: string,
  table: string,
  col: string,
  op: Op,
): boolean {
  const c = rules.users?.[user]?.tables?.[table]?.columns?.[col];
  return !!c?.deny?.includes(op);
}
