// ── RowEditorModal 的纯函数层（可单测，语义锁定在 row-editor-utils.test.ts）──
//
// 组件（RowEditorModal.tsx）只负责状态与提交编排；
// 值整形 / NULL 语义 / dirty 追踪全部沉淀在本文件，行为变更必须先过测试。

/** 列元信息（后端表结构接口形状） */
export interface ColumnInfo {
  name: string;
  type: string;
  not_null: boolean;
  pk?: boolean;
}

export type FieldValue = string | number | null;

/** 根据 SQL 类型推断前端 input 类型
 *
 * datetime 必须映射到 HTML5 真实存在的 "datetime-local"：
 * "datetime" 不是合法 input type，浏览器会静默回退成 text，
 * 秒精度随之被输入框丢弃（datetime-local 接受 YYYY-MM-DDTHH:MM[:SS]）
 */
export function getInputType(
  colType: string
): "number" | "text" | "date" | "datetime-local" | "boolean" {
  const upper = colType.toUpperCase();
  const base = upper.split("(")[0].trim();
  if (upper.includes("TINYINT(1)")) return "boolean";
  if (["BOOLEAN", "BOOL"].includes(base)) return "boolean";
  if (["INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"].includes(base)) return "number";
  if (["REAL", "DOUBLE", "FLOAT", "NUMERIC", "DECIMAL"].includes(base)) return "number";
  if (base === "DATE") return "date";
  if (["DATETIME", "TIMESTAMP"].includes(base)) return "datetime-local";
  return "text";
}

/** 将原始值转换为表单字符串
 *
 * datetime-local 保留到秒（YYYY-MM-DDTHH:MM[:SS]）：
 * 原始值带秒（"2024-01-01 10:30:45" / ISO 带时区前缀）时不许截断成分。
 */
export function valueToFormStr(val: unknown, inputType: string): string {
  if (val === null || val === undefined) return "";
  if (inputType === "datetime-local") {
    const s = String(val);
    const m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)/);
    if (m) return `${m[1]}T${m[2]}`;
    // 非标准格式兜底：尽量凑成 datetime-local 形状，截断到秒而不是分
    return s.replace(" ", "T").slice(0, 19);
  }
  return String(val);
}

/** 构建新增/更新行的值对象（主键列按 pk 标志排除；null 标记 → null；数字/布尔做类型整形）
 *
 * 服务两个模式，"空值"语义不同：
 * - 新增（默认）：空串 = 省略该字段，由列默认值/约束兜底
 * - 编辑（传 onlyKeys + keepEmpty）：只收集 onlyKeys 里的 dirty 字段，
 *   空串是合法值（用户把文本列清空为 ''），必须原样提交，不再丢弃
 */
export function buildRowValues(
  values: Record<string, FieldValue>,
  nullFlags: Record<string, boolean>,
  columns: ColumnInfo[],
  options: { onlyKeys?: Set<string>; keepEmpty?: boolean } = {}
): Record<string, unknown> {
  const { onlyKeys, keepEmpty = false } = options;
  const row: Record<string, unknown> = {};
  for (const col of columns) {
    if (col.pk === true) continue;
    if (onlyKeys && !onlyKeys.has(col.name)) continue;
    const isNull = nullFlags[col.name];
    if (isNull) {
      row[col.name] = null;
      continue;
    }
    const val = values[col.name];
    if (val === null) continue;
    if (val === "" && !keepEmpty) continue;
    const inputType = getInputType(col.type);
    if (inputType === "number") {
      const n = Number(val);
      row[col.name] = isNaN(n) ? val : n;
    } else if (inputType === "boolean") {
      row[col.name] = val === "1" || val === 1 ? 1 : 0;
    } else {
      row[col.name] = String(val);
    }
  }
  return row;
}

/** 编辑模式 dirty 字段集：值变了或 NULL 勾选状态变了的字段
 *
 * NULL 勾选语义：
 * - 新勾选 NULL（false→true）→ dirty，提交 null（无论输入框内容）
 * - 取消勾选 NULL（true→false）→ 输入了新值才算 dirty（提交新值）；
 *   没输入 = 保留原值，不进入 dirty 集
 *   （NULL 原值无法被改成 ''：取消勾选不输入时无法区分"想清空"与"没动"，
 *     是文档化取舍——想清空非 NULL 列请直接编辑成空串，想置 NULL 就保持勾选）
 *
 * 主键列（pk===true）恒排除：主键只用于定位行，不可编辑。
 */
export function getDirtyKeys(
  values: Record<string, FieldValue>,
  nullFlags: Record<string, boolean>,
  orig: { values: Record<string, FieldValue>; nulls: Record<string, boolean> },
  columns: ColumnInfo[]
): Set<string> {
  const dirty = new Set<string>();
  for (const col of columns) {
    if (col.pk === true) continue;
    const curNull = nullFlags[col.name] ?? false;
    const origNull = orig.nulls[col.name] ?? false;
    if (curNull) {
      if (!origNull) dirty.add(col.name);
      continue;
    }
    if (origNull) {
      if (values[col.name] !== "") dirty.add(col.name);
      continue;
    }
    if (values[col.name] !== orig.values[col.name]) dirty.add(col.name);
  }
  return dirty;
}
