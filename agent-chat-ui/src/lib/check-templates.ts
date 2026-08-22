// ── CHECK 约束模板分类系统（与后端 check_templates.py 对齐）──
// 设计：运行时按字段类型从后端拉取，本地缓存；输入参数时用本地 renderExprLocal 实时预览

import { apiFetch } from "@/lib/api-fetch";

export type CheckParamKind = "int" | "float" | "str" | "int_list" | "float_list" | "str_list";

/** 模板参数值的可选形态（输入框为 string，模板默认值可能为 number / string[]） */
export type CheckParamValue = string | number | string[];

export interface CheckParam {
  name: string;
  kind: CheckParamKind;
  placeholder: string;
  default: CheckParamValue;
}

export interface CheckTemplate {
  key: string;
  label: string;
  expr_template: string;
  desc: string;
  params: CheckParam[];
}

// 后端 /check-templates 响应的原始 JSON 形态（kind 是未收窄的字符串）
interface CheckParamJSON {
  name: string;
  kind: string;
  placeholder: string;
  default?: CheckParamValue;
}

interface CheckTemplateJSON {
  key: string;
  label: string;
  expr_template: string;
  desc: string;
  params?: CheckParamJSON[];
}

// 模块级缓存：normalize_type → 模板列表，避免重复请求后端
const checkTemplatesCache = new Map<string, CheckTemplate[]>();

// 前端类型归一化（必须与后端 check_templates.normalize_type 完全一致）
export function normalizeType(colType: string): string {
  if (!colType) return "TEXT";
  const raw = colType.toUpperCase().trim().split("(")[0].split(" ")[0].trim();
  const map: Record<string, string> = {
    INTEGER: "INTEGER", INT: "INTEGER", BIGINT: "INTEGER", SMALLINT: "INTEGER", TINYINT: "INTEGER", SERIAL: "INTEGER",
    REAL: "REAL", FLOAT: "REAL", DOUBLE: "REAL",
    NUMERIC: "NUMERIC", DECIMAL: "NUMERIC",
    TEXT: "TEXT", VARCHAR: "TEXT", CHAR: "TEXT", CLOB: "TEXT",
    DATE: "DATE", DATETIME: "DATETIME", TIMESTAMP: "DATETIME",
    BLOB: "BLOB", BOOLEAN: "INTEGER", BOOL: "INTEGER",
  };
  return map[raw] || "TEXT";
}

function parseTemplate(t: CheckTemplateJSON): CheckTemplate {
  return {
    key: t.key,
    label: t.label,
    expr_template: t.expr_template,
    desc: t.desc,
    params: (t.params || []).map((p) => ({
      name: p.name,
      kind: p.kind as CheckParamKind,
      placeholder: p.placeholder,
      default: p.default as CheckParamValue,
    })),
  };
}

// 按字段类型加载 CHECK 模板（带缓存）
export async function loadCheckTemplates(colType: string): Promise<CheckTemplate[]> {
  const nt = normalizeType(colType);
  const cached = checkTemplatesCache.get(nt);
  if (cached) return cached;
  try {
    const data = await apiFetch<{ templates?: CheckTemplateJSON[] }>(
      `/api/schema-graph/check-templates?type=${encodeURIComponent(colType)}`
    );
    const templates: CheckTemplate[] = (data.templates || []).map(parseTemplate);
    checkTemplatesCache.set(nt, templates);
    return templates;
  } catch {
    // 降级
  }
  return [{ key: "custom", label: "自定义", expr_template: "", desc: "手写 CHECK 表达式", params: [] }];
}

// 本地渲染单个参数值为 SQL 字面量（与后端 _render_param_value 对齐）
export function renderParam(value: CheckParamValue, kind: CheckParamKind): string {
  if (kind === "int") {
    const n = parseInt(String(value));
    return isNaN(n) ? "0" : String(n);
  }
  if (kind === "float") {
    const n = parseFloat(String(value));
    return isNaN(n) ? "0.0" : String(n);
  }
  if (kind === "str") return String(value);
  if (kind === "int_list" || kind === "float_list") {
    return toList(value)
      .map((v) => (kind === "int_list" ? parseInt(String(v)) : parseFloat(String(v))))
      .filter((n) => !isNaN(n))
      .join(", ");
  }
  if (kind === "str_list") {
    return toList(value)
      .map((v) => `'${String(v).replace(/'/g, "''")}'`)
      .join(", ");
  }
  return String(value);
}

export function toList(value: CheckParamValue): (string | number)[] {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") return value.split(",").map((s) => s.trim()).filter(Boolean);
  if (value === undefined || value === null) return [];
  return [value];
}

// 本地渲染 CHECK 表达式（与后端 render_expr 对齐）
export function renderExprLocal(
  template: CheckTemplate,
  colName: string,
  params: Record<string, CheckParamValue>,
): string {
  if (template.key === "custom" || !template.expr_template) return "";
  let expr = template.expr_template.replace(/\{col\}/g, colName);
  for (const p of template.params) {
    const v = params[p.name] ?? p.default;
    if (v === undefined || v === "") continue;
    expr = expr.replace(new RegExp(`\\{${p.name}\\}`, "g"), renderParam(v, p.kind));
  }
  return expr;
}
