// 权限规则 UI⇄API 映射的结构回归测试（node:test + tsx 执行，零新增依赖）
//
// 锁定契约：本文件断言的 JSON 形状即 PUT /api/permissions 的契约形状，
// 与后端 core/permission/policy.py 解析语义、data-engine 权限矩阵
// （scripts/_perm_matrix_check.py 的 ds_rules/tbl_rules/col_rules 产物）一一对应。
// 防的回归：表级勾选错写到库级、列节点残留空壳、白/黑名单键互斥被扩散合并破坏。
//
// 运行：npx tsx src/lib/perm-rules.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  OPS, scopeDenied, pruneShells, colDenied,
  withDsDenied, withTableDenied, withColDeny, withRoleRules,
  type RulesDoc,
} from "./perm-rules";

const ALL = new Set(OPS);

// ── scopeDenied：规则结构 → 复选框回显 ──

test("回显: 无覆盖/full = 全放（空禁止集）", () => {
  assert.deepStrictEqual(scopeDenied(undefined), new Set());
  assert.deepStrictEqual(scopeDenied({}), new Set(ALL)); // 裸壳 fail-closed
  assert.deepStrictEqual(scopeDenied({ mode: "full" }), new Set());
});

test("回显: read_only = 禁查询外全部", () => {
  assert.deepStrictEqual(
    scopeDenied({ mode: "read_only" }),
    new Set(["insert", "update", "delete", "ddl", "drop"]),
  );
});

test("回显: allow 白名单取补集（空白名单=全禁，对应 no_access 语义）", () => {
  assert.deepStrictEqual(
    scopeDenied({ allow: ["query", "insert"] }),
    new Set(["update", "delete", "ddl", "drop"]),
  );
  assert.deepStrictEqual(scopeDenied({ allow: [] }), new Set(ALL));
});

test("回显: deny 黑名单原样", () => {
  assert.deepStrictEqual(scopeDenied({ deny: ["delete", "drop"] }), new Set(["delete", "drop"]));
});

// ── withDsDenied：库级勾选 → JSON ──

test("库级勾选: 勾 delete → custom 黑名单（矩阵 ds_rules 形状）", () => {
  const r = withDsDenied({}, "legacy", new Set(["delete"]));
  assert.deepStrictEqual(r, { datasources: { legacy: { mode: "custom", deny: ["delete"] } } });
});

test("库级勾选: 全部取消 → 节点删除（跟随全局默认）", () => {
  const r0 = withDsDenied({}, "legacy", new Set(["delete"]));
  const r1 = withDsDenied(r0, "legacy", new Set());
  assert.deepStrictEqual(r1, { datasources: {} });
});

test("库级勾选: 取消但有 tables → 保留壳（壳向上继承，不吞子规则）", () => {
  const withTable = withTableDenied({}, "legacy", "t1", new Set(["delete"]));
  const r = withDsDenied(withTable, "legacy", new Set());
  assert.deepStrictEqual(r, {
    datasources: { legacy: { tables: { t1: { mode: "custom", deny: ["delete"] } } } },
  });
});

test("库级勾选: 旧 read_only 重勾 → 旧 mode 被清（防扩散合并带回历史键）", () => {
  const r0: RulesDoc = { datasources: { legacy: { mode: "read_only" } } };
  const r1 = withDsDenied(r0, "legacy", new Set(["drop"]));
  assert.deepStrictEqual(r1, { datasources: { legacy: { mode: "custom", deny: ["drop"] } } });
});

// ── withTableDenied：表级勾选 → JSON（核心防回归：不错位到库级）──

test("表级勾选: 写入 tables 节点，库级不带 mode/deny（矩阵 tbl_rules 形状）", () => {
  const r = withTableDenied({}, "primary", "quota_items", new Set(["delete"]));
  // 库级 scope 不被污染（deepStrictEqual 的类型窄化会把 r 锁成字面量类型，此检查须在其前）
  assert.ok(!("mode" in r.datasources!.primary) && !("deny" in r.datasources!.primary));
  assert.deepStrictEqual(r, {
    datasources: { primary: { tables: { quota_items: { mode: "custom", deny: ["delete"] } } } },
  });
});

test("表级勾选: 取消且有 columns → 保留壳；无 columns → 表节点删除", () => {
  const withCol = withColDeny({}, "primary", "t1", "secret", "query", true);
  const r1 = withTableDenied(withCol, "primary", "t1", new Set());
  assert.deepStrictEqual(r1, {
    datasources: { primary: { tables: { t1: { columns: { secret: { deny: ["query"] } } } } } },
  });
  const r2 = withTableDenied(
    withTableDenied({}, "primary", "t1", new Set(["delete"])), "primary", "t1", new Set());
  assert.deepStrictEqual(r2, { datasources: { primary: { tables: {} } } });
});

// ── withColDeny：列级勾选 → JSON（矩阵 col_rules 形状）──

test("列级勾选: 禁 query → columns.<col>.deny=[query]，回读一致", () => {
  const r = withColDeny({}, "primary", "t1", "secret", "query", true);
  assert.deepStrictEqual(r, {
    datasources: { primary: { tables: { t1: { columns: { secret: { deny: ["query"] } } } } } },
  });
  assert.equal(colDenied(r, "primary", "t1", "secret", "query"), true);
  assert.equal(colDenied(r, "primary", "t1", "secret", "update"), false);
});

test("列级勾选: 叠加第二操作 → 同列 deny 累加；取消到最后一个 → 列节点删除", () => {
  let r = withColDeny({}, "primary", "t1", "secret", "query", true);
  r = withColDeny(r, "primary", "t1", "secret", "update", true);
  assert.deepStrictEqual(
    r.datasources!.primary.tables!.t1.columns!.secret.deny, ["query", "update"]);
  r = withColDeny(r, "primary", "t1", "secret", "query", false);
  r = withColDeny(r, "primary", "t1", "secret", "update", false);
  assert.deepStrictEqual(
    r.datasources!.primary.tables!.t1.columns, {}); // 空壳留待 pruneShells 摘
});

// ── withRoleRules：角色编辑（allow/deny 互斥）──

test("角色: deny 切 allow 清掉旧 deny（互斥）；清空名单 → 角色删除", () => {
  let r = withRoleRules({}, "readonly", { deny: ["delete", "drop"] });
  assert.deepStrictEqual(r, { roles: { readonly: { deny: ["delete", "drop"] } } });
  r = withRoleRules(r, "readonly", { allow: ["query"] });
  assert.deepStrictEqual(r, { roles: { readonly: { allow: ["query"] } } }); // deny 已清
  r = withRoleRules(r, "readonly", {});
  assert.deepStrictEqual(r, { roles: {} });
});

// ── pruneShells：保存前摘壳（文件卫生，不改语义）──

test("摘壳: 裸壳数据源摘除，有子规则的壳保留", () => {
  const r: RulesDoc = {
    datasources: {
      empty_shell: {},
      has_table: { tables: { t1: { columns: { c1: { deny: ["query"] } } } } },
    },
  };
  assert.deepStrictEqual(pruneShells(r), {
    datasources: { has_table: { tables: { t1: { columns: { c1: { deny: ["query"] } } } } } },
  });
});

test("摘壳: 空 deny 列、空 tables、空 roles 全部摘除；有效规则原样通过", () => {
  const r: RulesDoc = {
    default: "full",
    datasources: {
      primary: {
        tables: {
          t1: { columns: { c1: { deny: [] } } },   // 空 deny → 整表摘除
          t2: { mode: "custom", deny: ["ddl"] },   // 有效 → 保留
        },
      },
    },
    roles: { ghost: {} },                          // 无 allow/deny → 摘除
  };
  assert.deepStrictEqual(pruneShells(r), {
    default: "full",
    datasources: { primary: { tables: { t2: { mode: "custom", deny: ["ddl"] } } } },
    roles: {},
  });
});

// ── 端到端形状契约：UI 勾选产物 == 权限矩阵用例的规则形状 ──
// （矩阵 G1/G2/G3 已验证这些形状在后端的拦截语义）

test("端到端: UI 操作链 → prune → 与矩阵 col_rules(['query']) 形状逐字节一致", () => {
  let r: RulesDoc = { default: "full", roles: {} };
  r = withColDeny(r, "primary", "t1", "secret", "query", true);
  r = withColDeny(r, "primary", "t1", "secret", "update", true);
  assert.deepStrictEqual(pruneShells(r), {
    default: "full",
    roles: {},
    datasources: {
      primary: { tables: { t1: { columns: { secret: { deny: ["query", "update"] } } } } },
    },
  });
});

test("端到端: 表级禁 delete + 库级 read_only 意图（勾五留一）→ 矩阵形状", () => {
  // UI 上"read_only"的表达 = 勾掉 query 外全部（scopeDenied 回显语义的对偶）
  const roSet = new Set(OPS.filter((o) => o !== "query"));
  let r = withDsDenied({}, "legacy", roSet);
  r = withTableDenied(r, "primary", "quota_items", new Set(["delete"]));
  assert.deepStrictEqual(pruneShells(r), {
    datasources: {
      legacy: { mode: "custom", deny: ["insert", "update", "delete", "ddl", "drop"] },
      primary: { tables: { quota_items: { mode: "custom", deny: ["delete"] } } },
    },
  });
});
