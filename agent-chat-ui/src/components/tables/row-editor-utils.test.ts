// RowEditorModal 纯函数层的单元测试（node:test + tsx 执行，零新增依赖）
//
// 锁定契约：buildRowValues 的值整形/空值语义、getDirtyKeys 的 dirty 三态矩阵。
// 防的回归：编辑模式把未改动列误提交、空串被当"省略"丢弃、NULL 勾选方向搞反、
// 主键列被写进更新集（主键只用于定位行）。
//
// 运行：npx tsx src/components/tables/row-editor-utils.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  getInputType,
  valueToFormStr,
  buildRowValues,
  getDirtyKeys,
  type ColumnInfo,
  type FieldValue,
} from "./row-editor-utils";

// 测试夹具：uid 是主键（名字故意不叫 id——主键列名任意，排除逻辑只看 pk 标志）
const COLS: ColumnInfo[] = [
  { name: "uid", type: "INTEGER", not_null: true, pk: true },
  { name: "name", type: "TEXT", not_null: true },
  { name: "age", type: "INTEGER", not_null: false },
  { name: "active", type: "BOOLEAN", not_null: false },
  { name: "note", type: "TEXT", not_null: false },
];

type Snap = {
  values: Record<string, FieldValue>;
  nulls: Record<string, boolean>;
};
const snap = (values: Snap["values"], nulls: Snap["nulls"] = {}): Snap => ({
  values,
  nulls,
});

// 组件不变式：values 在初始化时为每列都建档（缺省 ""），测试按同一契约构造完整值表
const vals = (
  over: Record<string, FieldValue>,
): Record<string, FieldValue> => ({
  uid: "",
  name: "",
  age: "",
  active: "",
  note: "",
  ...over,
});

// ── buildRowValues：新增模式（空=省略，类型整形，NULL 勾选，主键排除）──

test("新增: 空串=省略该字段（由列默认值/约束兜底），非空原样收集", () => {
  const row = buildRowValues(vals({ name: "张三", active: "1" }), {}, COLS);
  assert.deepStrictEqual(row, { name: "张三", active: 1 });
});

test("新增: 数字列整形为 number，非数字输入原样保留（交由后端 422 报错）", () => {
  const row = buildRowValues(vals({ name: "x", age: "42" }), {}, COLS);
  assert.deepStrictEqual(row, { name: "x", age: 42 });
  const bad = buildRowValues(vals({ name: "x", age: "abc" }), {}, COLS);
  assert.deepStrictEqual(bad, { name: "x", age: "abc" });
});

test("新增: 布尔列 1/'1'→1，其余→0", () => {
  assert.deepStrictEqual(
    buildRowValues(vals({ name: "x", active: "1" }), {}, COLS),
    { name: "x", active: 1 },
  );
  assert.deepStrictEqual(
    buildRowValues(vals({ name: "x", active: 1 }), {}, COLS),
    { name: "x", active: 1 },
  );
  assert.deepStrictEqual(
    buildRowValues(vals({ name: "x", active: "0" }), {}, COLS),
    { name: "x", active: 0 },
  );
});

test("新增: 勾选 NULL → 提交 null（即使输入框有内容，null 标记优先）", () => {
  const row = buildRowValues(
    vals({ name: "x", age: "30", note: "有内容" }),
    { age: true, note: true },
    COLS,
  );
  assert.deepStrictEqual(row, { name: "x", age: null, note: null });
});

test("新增: 主键列 uid 按 pk 标志排除（不进入插入值）", () => {
  const row = buildRowValues(vals({ uid: "99", name: "x" }), {}, COLS);
  assert.deepStrictEqual(row, { name: "x" });
});

// ── getDirtyKeys：编辑模式 dirty 三态矩阵 ──

test("编辑: 值变更 → dirty；未修改提交 → 空集", () => {
  const orig = snap({
    uid: "1",
    name: "张三",
    age: "30",
    active: "1",
    note: "旧",
  });
  const unchanged = getDirtyKeys(
    { uid: "1", name: "张三", age: "30", active: "1", note: "旧" },
    {},
    orig,
    COLS,
  );
  assert.deepStrictEqual(unchanged, new Set());
  const changed = getDirtyKeys(
    { uid: "1", name: "李四", age: "30", active: "1", note: "旧" },
    {},
    orig,
    COLS,
  );
  assert.deepStrictEqual(changed, new Set(["name"]));
});

test("编辑: 空串是合法值——文本列清空为 '' 是 dirty，须原样提交（keepEmpty）", () => {
  const orig = snap({
    uid: "1",
    name: "张三",
    age: "30",
    active: "1",
    note: "旧备注",
  });
  const dirty = getDirtyKeys(
    { uid: "1", name: "张三", age: "30", active: "1", note: "" },
    {},
    orig,
    COLS,
  );
  assert.deepStrictEqual(dirty, new Set(["note"]));
  // keepEmpty 语义：dirty 的空串必须进提交集，不能被当"省略"丢弃
  const row = buildRowValues(
    { uid: "1", name: "张三", age: "30", active: "1", note: "" },
    {},
    COLS,
    { onlyKeys: dirty, keepEmpty: true },
  );
  assert.deepStrictEqual(row, { note: "" });
});

test("编辑: NULL 勾选双向——勾选（false→true）dirty；保持勾选且未动 不 dirty", () => {
  const orig = snap({
    uid: "1",
    name: "x",
    age: "30",
    active: "1",
    note: "旧",
  });
  // 新勾选 NULL → dirty，提交 null（无论输入框内容）
  const on = getDirtyKeys(
    { uid: "1", name: "x", age: "30", active: "1", note: "旧" },
    { note: true },
    orig,
    COLS,
  );
  assert.deepStrictEqual(on, new Set(["note"]));
  // 原本就是 NULL、保持勾选 → 不 dirty
  const origNull = snap(
    { uid: "1", name: "x", age: "30", active: "1", note: "" },
    { note: true },
  );
  const stay = getDirtyKeys(
    { uid: "1", name: "x", age: "30", active: "1", note: "" },
    { note: true },
    origNull,
    COLS,
  );
  assert.deepStrictEqual(stay, new Set());
});

test("编辑: 取消 NULL 勾选——不输入=不 dirty（NULL→'' 不可达是文档化取舍）；输入新值=dirty", () => {
  const origNull = snap(
    { uid: "1", name: "x", age: "30", active: "1", note: "" },
    { note: true },
  );
  // 取消勾选但不输入：保留库中 NULL 原值，不进 dirty 集
  const noInput = getDirtyKeys(
    { uid: "1", name: "x", age: "30", active: "1", note: "" },
    { note: false },
    origNull,
    COLS,
  );
  assert.deepStrictEqual(noInput, new Set());
  // 取消勾选并输入新值：dirty，提交新值
  const withInput = getDirtyKeys(
    { uid: "1", name: "x", age: "30", active: "1", note: "新备注" },
    { note: false },
    origNull,
    COLS,
  );
  assert.deepStrictEqual(withInput, new Set(["note"]));
});

test("编辑: 主键列 uid 恒排除——即使快照与当前值不一致也不 dirty", () => {
  const orig = snap({
    uid: "1",
    name: "x",
    age: "30",
    active: "1",
    note: "旧",
  });
  const dirty = getDirtyKeys(
    { uid: "2", name: "x", age: "30", active: "1", note: "旧" },
    {},
    orig,
    COLS,
  );
  assert.deepStrictEqual(dirty, new Set());
});

// ── 编辑提交链路：onlyKeys 收敛 + 整形一致性 ──

test("编辑提交: buildRowValues(onlyKeys) 只收集 dirty 字段，数字列同样整形", () => {
  const dirty = new Set(["age"]);
  const row = buildRowValues(
    { uid: "1", name: "张三", age: "31", active: "1", note: "旧" },
    {},
    COLS,
    { onlyKeys: dirty, keepEmpty: true },
  );
  assert.deepStrictEqual(row, { age: 31 });
});

test("编辑提交: dirty 集含 NULL 勾选字段 → 提交 null", () => {
  const dirty = new Set(["note"]);
  const row = buildRowValues(
    { uid: "1", name: "x", age: "30", active: "1", note: "" },
    { note: true },
    COLS,
    { onlyKeys: dirty, keepEmpty: true },
  );
  assert.deepStrictEqual(row, { note: null });
});

// ── datetime-local 类型与秒精度（HTML5 无 datetime 类型，回退 text 会丢秒）──

test("getInputType: DATETIME/TIMESTAMP → datetime-local；DATE → date", () => {
  assert.equal(getInputType("DATETIME"), "datetime-local");
  assert.equal(getInputType("timestamp"), "datetime-local");
  assert.equal(getInputType("DATE"), "date");
});

test("valueToFormStr: datetime-local 保留到秒，空格分隔转 T；null → 空串", () => {
  assert.equal(
    valueToFormStr("2024-01-01 10:30:45", "datetime-local"),
    "2024-01-01T10:30:45",
  );
  assert.equal(
    valueToFormStr("2024-01-01T10:30", "datetime-local"),
    "2024-01-01T10:30",
  );
  assert.equal(valueToFormStr(null, "datetime-local"), "");
});
