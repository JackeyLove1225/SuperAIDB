"use client";

import { useState, useEffect, useRef } from "react";
import type { ColumnData } from "./table-node";
import { apiFetch } from "@/lib/api-fetch";
import {
  normalizeType,
  loadCheckTemplates,
  renderExprLocal,
  type CheckTemplate,
  type CheckParamValue,
} from "@/lib/check-templates";

// ── 字段类型说明映射（需求2：悬停显示使用场景）──
const FIELD_TYPES: { name: string; desc: string }[] = [
  { name: "INTEGER", desc: "整数类型。适用于计数、ID、状态码等不带小数的数值。如年龄、数量、外键引用 id。" },
  { name: "TEXT", desc: "文本类型。适用于名称、描述、地址等字符串。如用户名、商品标题、备注。" },
  { name: "REAL", desc: "浮点类型。适用于带小数的数值，精度要求不高。如温度、评分、比例。" },
  { name: "NUMERIC", desc: "数值类型。适用于需要精确计算的小数，如金额、汇率。优先用 REAL 替代以简化。" },
  { name: "DATE", desc: "日期类型。仅存储日期（年-月-日）。如出生日期、创建日期。存储为 TEXT 格式 'YYYY-MM-DD'。" },
  { name: "DATETIME", desc: "日期时间类型。存储日期+时间。如订单时间、最后登录时间。存储为 TEXT 格式 'YYYY-MM-DD HH:MM:SS'。" },
  { name: "BLOB", desc: "二进制大对象。适用于存储图片、文件等二进制数据。一般不建议直接存数据库，推荐存路径。" },
];

// ── 约束选项说明（需求5：悬停显示含义及使用场景）──
const CONSTRAINT_INFO: { key: "not_null" | "is_unique" | "is_indexed"; label: string; desc: string; color: string }[] = [
  {
    key: "not_null",
    label: "非空",
    desc: "NOT NULL 约束：该字段不允许为空值。适用于必须填写的字段，如用户名、邮箱、订单金额等关键字段。",
    color: "text-red-600",
  },
  {
    key: "is_unique",
    label: "唯一",
    desc: "UNIQUE 约束：该字段的值在整张表中不能重复。适用于手机号、邮箱、工号等需要唯一标识的字段。",
    color: "text-zinc-600",
  },
  {
    key: "is_indexed",
    label: "索引",
    desc: "普通索引：为该字段创建索引以加速查询。适用于经常作为查询条件（WHERE/JOIN）的字段，如外键、状态码。会占用额外存储，写操作略慢。",
    color: "text-zinc-600",
  },
];

/** onUpdate 签名：泛型保证 field 与 value 类型对应 */
export type ColumnUpdateFn = <K extends keyof ColumnData>(field: K, value: ColumnData[K]) => void;

// ============================================================
// 单字段编辑器——含描述框/类型tooltip/约束复选框/CHECK编辑器
// ============================================================

export default function ColumnEditor({
  column: col,
  isIdField,
  allColumnNames,
  onUpdate,
  onRemove,
}: {
  column: ColumnData;
  isIdField: boolean;
  allColumnNames: string[];
  onUpdate: ColumnUpdateFn;
  onRemove: () => void;
}) {
  const [showCheckEditor, setShowCheckEditor] = useState(!!col.check_constraint);
  // 当前字段类型对应的模板列表（从后端按类型加载）
  const [templates, setTemplates] = useState<CheckTemplate[]>([]);
  // 选中的模板 key（与后端 check_template_key 对齐）
  const [checkTemplateKey, setCheckTemplateKey] = useState<string>(
    col.check_template_key || (col.check_constraint ? "custom" : "")
  );
  // 模板参数（与后端 check_template_params 对齐）
  const [checkParams, setCheckParams] = useState<Record<string, CheckParamValue>>(
    col.check_template_params || {}
  );
  // 软提示：用户主动点"校验"按钮后显示的结果
  const [validateMsg, setValidateMsg] = useState<{ ok: boolean; msg: string } | null>(null);
  const [validating, setValidating] = useState(false);

  // 类型变化时加载该类型的模板列表（带缓存）
  useEffect(() => {
    if (!showCheckEditor) return;
    loadCheckTemplates(col.type).then(setTemplates);
  }, [col.type, showCheckEditor]);

  // 类型变化时清空模板选择（不同类型模板不通用），但保留 custom 的表达式
  // 用 ref 记录上一次类型，避免初次渲染也触发清空
  const prevTypeRef = useRef<string>(col.type);
  useEffect(() => {
    if (prevTypeRef.current !== col.type) {
      // 类型真的变了
      if (checkTemplateKey !== "custom" && checkTemplateKey !== "") {
        // 原来用的是模板（非 custom），新类型下旧模板可能不适用，清空
        setCheckTemplateKey("");
        setCheckParams({});
        setValidateMsg(null);
        onUpdate("check_constraint", "");
        onUpdate("check_template_key", "");
        onUpdate("check_template_params", {});
      }
      prevTypeRef.current = col.type;
    }
    // onUpdate 每次渲染都是新引用，但 prevTypeRef 守卫保证只有类型真变时才执行
  }, [col.type, checkTemplateKey, onUpdate]);

  // 字段名变化时重新渲染表达式（仅对模板模式生效）
  useEffect(() => {
    if (checkTemplateKey === "custom" || !checkTemplateKey) return;
    const tmpl = templates.find((t) => t.key === checkTemplateKey);
    if (!tmpl) return;
    const rendered = renderExprLocal(tmpl, col.name, checkParams);
    if (rendered !== col.check_constraint) {
      onUpdate("check_constraint", rendered);
    }
    // rendered !== col.check_constraint 守卫防止重复回写；模板模式下表达式始终由参数派生
  }, [col.name, col.check_constraint, checkTemplateKey, templates, checkParams, onUpdate]);

  const handleTemplateSelect = (key: string) => {
    setCheckTemplateKey(key);
    setValidateMsg(null);
    if (key === "custom") {
      // 切到自定义：保留现有 check_constraint，标记 key
      onUpdate("check_template_key", "custom");
      return;
    }
    const tmpl = templates.find((t) => t.key === key);
    if (!tmpl) return;
    // 用模板默认值初始化参数
    const defaultParams: Record<string, CheckParamValue> = {};
    for (const p of tmpl.params) {
      defaultParams[p.name] = p.default;
    }
    setCheckParams(defaultParams);
    onUpdate("check_template_key", key);
    onUpdate("check_template_params", defaultParams);
    // 渲染表达式
    const rendered = renderExprLocal(tmpl, col.name, defaultParams);
    onUpdate("check_constraint", rendered);
  };

  const handleParamChange = (paramName: string, value: string) => {
    const newParams = { ...checkParams, [paramName]: value };
    setCheckParams(newParams);
    onUpdate("check_template_params", newParams);
    // 实时渲染表达式
    const tmpl = templates.find((t) => t.key === checkTemplateKey);
    if (tmpl) {
      const rendered = renderExprLocal(tmpl, col.name, newParams);
      onUpdate("check_constraint", rendered);
    }
  };

  // 软提示：调用后端 validate-check 接口（不阻断输入）
  const handleValidate = async () => {
    if (!col.check_constraint) return;
    setValidating(true);
    setValidateMsg(null);
    try {
      const data = await apiFetch<{ ok: boolean; message?: string }>(
        "/api/schema-graph/validate-check",
        {
          method: "POST",
          body: JSON.stringify({
            expr: col.check_constraint,
            col_name: col.name,
            col_type: col.type,
            table_columns: allColumnNames,
          }),
        },
      );
      setValidateMsg({
        ok: data.ok,
        msg: data.ok ? "✓ 表达式合法，可安全保存" : (data.message ?? "表达式非法"),
      });
    } catch (e) {
      setValidateMsg({ ok: false, msg: `校验请求失败: ${e}` });
    } finally {
      setValidating(false);
    }
  };

  const selectedTemplate = templates.find((t) => t.key === checkTemplateKey);

  return (
    <div className={`rounded-md border p-2 ${
      isIdField
        ? "border-amber-300 bg-amber-50/50 dark:border-amber-700 dark:bg-amber-950/20"
        : "border-zinc-200 bg-zinc-50/50 dark:border-zinc-700 dark:bg-zinc-800/30"
    }`}>
      {/* 第1行：字段名 + 类型下拉（带tooltip） + 删除按钮 —— min-w-0 修复横向滚动 */}
      <div className="flex items-center gap-1.5 min-w-0">
        <input
          type="text"
          value={col.name}
          onChange={(e) => onUpdate("name", e.target.value)}
          disabled={isIdField}
          className="min-w-0 flex-1 rounded border border-zinc-300 px-2 py-1 font-mono text-xs break-all disabled:bg-amber-100/50 dark:border-zinc-700 dark:bg-zinc-800 dark:disabled:bg-amber-950/30"
          placeholder="字段名"
        />
        <TypeSelectWithTooltip
          value={col.type}
          onChange={(v) => onUpdate("type", v)}
          disabled={isIdField}
        />
        {isIdField && (
          <span
            className="shrink-0 rounded bg-amber-200 px-1.5 py-1 text-xs font-bold text-amber-800 dark:bg-amber-800 dark:text-amber-100"
            title="主键"
          >
            🔑 PK
          </span>
        )}
        <button
          onClick={onRemove}
          disabled={isIdField}
          className="shrink-0 rounded px-1 text-xs text-red-500 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-20 dark:hover:bg-red-950"
          title={isIdField ? "id 主键不可删除" : "删除字段"}
        >
          ✕
        </button>
      </div>

      {/* 第2行：字段描述框（需求1） */}
      <input
        type="text"
        value={col.description || ""}
        onChange={(e) => onUpdate("description", e.target.value)}
        className="mt-1.5 w-full rounded border border-zinc-200 px-2 py-1 text-xs text-zinc-600 placeholder:text-zinc-400 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400"
        placeholder="📝 字段描述（简要说明该字段的用途）"
      />

      {/* 第3行：约束复选框（需求5）+ CHECK开关（需求6）—— id 字段不显示 */}
      {!isIdField && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          {CONSTRAINT_INFO.map((c) => (
            <ConstraintCheckbox
              key={c.key}
              label={c.label}
              desc={c.desc}
              color={c.color}
              checked={col[c.key] || false}
              onChange={(v) => onUpdate(c.key, v)}
            />
          ))}
          <ConstraintCheckbox
            key="check"
            label="条件"
            desc="CHECK 约束：限制字段值必须满足特定条件。如数量必须大于0、状态只能是0或1等。点击勾选后会展开条件编辑器，模板会根据字段类型自动适配。"
            color="text-zinc-600"
            checked={showCheckEditor}
            onChange={(v) => {
              setShowCheckEditor(v);
              if (!v) {
                onUpdate("check_constraint", "");
                onUpdate("check_template_key", "");
                onUpdate("check_template_params", {});
                setCheckTemplateKey("");
                setCheckParams({});
                setValidateMsg(null);
              }
            }}
          />
        </div>
      )}

      {/* 第4行：CHECK 约束编辑器（按字段类型动态加载模板 + 参数化输入 + 软提示） */}
      {!isIdField && showCheckEditor && (
        <div className="mt-1.5 rounded border border-green-200 bg-green-50/50 p-2 dark:border-green-800 dark:bg-green-950/20">
          {/* 模板选择下拉框 */}
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-xs font-medium text-green-700 dark:text-green-400">CHECK</span>
            <select
              value={checkTemplateKey}
              onChange={(e) => handleTemplateSelect(e.target.value)}
              className="min-w-0 flex-1 rounded border border-green-300 bg-white px-1.5 py-1 text-xs dark:border-green-700 dark:bg-zinc-800"
            >
              <option value="">选择模板（按类型 {normalizeType(col.type)}）...</option>
              {templates.map((t) => (
                <option key={t.key} value={t.key} title={t.desc}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>

          {/* 模板使用说明 */}
          {selectedTemplate && selectedTemplate.desc && checkTemplateKey !== "custom" && (
            <div className="mt-1 rounded bg-blue-50 px-2 py-1 text-[10px] leading-relaxed text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
              💡 {selectedTemplate.desc}
            </div>
          )}

          {/* 参数化输入框（仅当模板有参数且非 custom 时显示） */}
          {selectedTemplate && selectedTemplate.params.length > 0 && checkTemplateKey !== "custom" && (
            <div className="mt-1.5 grid grid-cols-2 gap-1.5">
              {selectedTemplate.params.map((p) => (
                <div key={p.name} className="flex min-w-0 items-center gap-1">
                  <label className="shrink-0 text-[10px] font-mono text-zinc-600 dark:text-zinc-400">
                    {p.name}:
                  </label>
                  <input
                    type={p.kind === "int" || p.kind === "float" ? "number" : "text"}
                    step={p.kind === "float" ? "any" : undefined}
                    value={checkParams[p.name] ?? p.default ?? ""}
                    onChange={(e) => handleParamChange(p.name, e.target.value)}
                    className="min-w-0 flex-1 rounded border border-green-300 bg-white px-1.5 py-0.5 font-mono text-xs dark:border-green-700 dark:bg-zinc-800"
                    placeholder={p.placeholder}
                  />
                </div>
              ))}
            </div>
          )}

          {/* 自定义表达式输入框 */}
          {checkTemplateKey === "custom" && (
            <input
              type="text"
              value={col.check_constraint || ""}
              onChange={(e) => {
                onUpdate("check_constraint", e.target.value);
                setValidateMsg(null);
              }}
              className="mt-1.5 w-full rounded border border-green-300 bg-white px-2 py-1 font-mono text-xs dark:border-green-700 dark:bg-zinc-800"
              placeholder="如: age >= 0 AND age <= 150（支持跨列：end_date > start_date）"
            />
          )}

          {/* 渲染后的 CHECK 表达式 + 软提示按钮 */}
          {col.check_constraint && (
            <div className="mt-1.5 flex min-w-0 items-start gap-1.5">
              <div className="min-w-0 flex-1 rounded bg-green-100 px-2 py-0.5 font-mono text-xs break-all text-green-800 dark:bg-green-900/40 dark:text-green-300">
                ✓ CHECK ({col.check_constraint})
              </div>
              <button
                onClick={handleValidate}
                disabled={validating}
                className="shrink-0 rounded border border-green-400 bg-white px-1.5 py-0.5 text-[10px] text-green-700 hover:bg-green-50 disabled:opacity-50 dark:border-green-700 dark:bg-zinc-800 dark:text-green-400 dark:hover:bg-green-950/30"
                title="调用后端校验表达式安全性（不阻断输入，保存时会再次硬校验）"
              >
                {validating ? "..." : "🔍 校验"}
              </button>
            </div>
          )}

          {/* 软提示结果（用户主动触发才显示，不阻断输入） */}
          {validateMsg && (
            <div className={`mt-1 rounded px-2 py-0.5 text-[10px] ${
              validateMsg.ok
                ? "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300"
                : "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300"
            }`}>
              {validateMsg.msg}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================
// 字段类型下拉框——带 hover tooltip（需求2）
// ============================================================

function TypeSelectWithTooltip({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const [showTip, setShowTip] = useState(false);
  const currentDesc = FIELD_TYPES.find((t) => t.name === value)?.desc || "未知类型";

  return (
    <div className="relative shrink-0">
      <div className="flex items-center gap-0.5">
        {/* 原生 select——浏览器原生下拉层，不受 overflow/z-index 影响，onChange 一定触发 */}
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          className="w-20 rounded border border-zinc-300 bg-white px-1 py-1 text-xs disabled:bg-amber-100/50 dark:border-zinc-700 dark:bg-zinc-800 dark:disabled:bg-amber-950/30"
        >
          {FIELD_TYPES.map((t) => (
            <option key={t.name} value={t.name} title={t.desc}>
              {t.name}
            </option>
          ))}
        </select>
        {/* ? 按钮——toggle 显示当前类型的详细说明（保留需求2的 tooltip 功能） */}
        <button
          type="button"
          className="shrink-0 rounded px-0.5 text-xs text-amber-500 hover:bg-amber-50 dark:hover:bg-amber-950/30"
          onMouseEnter={() => setShowTip(true)}
          onMouseLeave={() => setShowTip(false)}
          onClick={() => setShowTip(!showTip)}
          title="查看类型说明"
        >
          ?
        </button>
      </div>
      {/* tooltip——显示当前选中类型的说明（hover ? 或点击 ? 切换） */}
      {showTip && (
        <div className="absolute left-0 top-full z-50 mt-1 w-56 rounded border border-amber-200 bg-amber-50 p-2 text-xs leading-relaxed text-amber-900 shadow-lg dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200">
          <div className="mb-1 font-bold">{value}</div>
          <div>{currentDesc}</div>
        </div>
      )}
    </div>
  );
}

// ============================================================
// 约束复选框——带 hover tooltip（需求5）
// ============================================================

function ConstraintCheckbox({
  label,
  desc,
  color,
  checked,
  onChange,
}: {
  label: string;
  desc: string;
  color: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="group relative">
      <label className="flex cursor-pointer items-center gap-0.5 text-xs">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="h-3 w-3"
        />
        <span className={color}>{label}</span>
      </label>
      {/* 悬浮提示框（需求5）：鼠标悬停显示含义及使用场景，挪开消失 */}
      <div className="pointer-events-none absolute bottom-full left-0 z-20 mb-1 hidden w-52 rounded bg-zinc-800 p-2 text-xs leading-relaxed text-white shadow-lg group-hover:block">
        {desc}
      </div>
    </div>
  );
}
