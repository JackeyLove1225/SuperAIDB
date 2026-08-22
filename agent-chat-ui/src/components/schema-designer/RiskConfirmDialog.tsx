"use client";

import DraggableModal from "@/components/ui/draggable-modal";
import type { RiskReport, RiskChange } from "./types";

// 风险等级样式映射
const RISK_STYLES: Record<string, { bg: string; text: string; border: string; icon: string; label: string }> = {
  danger:  { bg: "bg-red-50 dark:bg-red-950/30",    text: "text-red-700 dark:text-red-400",    border: "border-red-300 dark:border-red-800",    icon: "🔴", label: "高危" },
  warning: { bg: "bg-amber-50 dark:bg-amber-950/30", text: "text-amber-700 dark:text-amber-400", border: "border-amber-300 dark:border-amber-800", icon: "🟡", label: "警告" },
  safe:    { bg: "bg-green-50 dark:bg-green-950/30", text: "text-green-700 dark:text-green-400", border: "border-green-300 dark:border-green-800", icon: "🟢", label: "安全" },
};

// 变更类型中文映射
const CHANGE_TYPE_LABELS: Record<string, string> = {
  add_column: "新增字段",
  drop_column: "删除字段",
  modify_type: "类型变更",
  rename_column: "字段重命名",
  add_not_null: "加严非空",
  drop_not_null: "放宽非空",
  add_unique: "加严唯一",
  drop_unique: "放宽唯一",
  add_check: "新增CHECK",
  drop_check: "删除CHECK",
  modify_precision: "精度变更",
  drop_fk: "删除外键",
  add_fk: "新增外键",
  rename_table: "表重命名",
  modify_pk: "主键变更",
};

/** 风险确认对话框——显示变更风险评估报告，供用户二次确认 */
export default function RiskConfirmDialog({
  report,
  onCancel,
  onConfirm,
}: {
  report: RiskReport;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const riskLevel: string = report.risk_level || "safe";
  const style = RISK_STYLES[riskLevel] || RISK_STYLES.safe;
  const changes: RiskChange[] = report.changes || [];

  // 问题5：使用 DraggableModal 支持拖拽+调整大小
  return (
    <DraggableModal
      title={
        <span className="flex items-center gap-2">
          <span className="text-xl">{style.icon}</span>
          <span>
            变更风险确认
            <span className={`ml-2 text-sm font-normal ${style.text}`}>
              风险等级：{style.label} · {report.summary || `${changes.length} 项变更`}
            </span>
          </span>
        </span>
      }
      onClose={onCancel}
      initialWidth={700}
      zIndex={60}
      titleClassName={`${style.border} ${style.bg}`}
    >
      {/* 变更详情列表 */}
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        以下变更可能影响数据完整性，请确认后强制执行：
      </p>
      <div className="space-y-2">
        {changes.map((c, i) => {
          const cStyle = RISK_STYLES[c.risk] || RISK_STYLES.safe;
          const label = CHANGE_TYPE_LABELS[c.type] || c.type;
          return (
            <div
              key={i}
              className={`rounded-md border p-3 ${cStyle.border} ${cStyle.bg}`}
            >
              <div className="flex items-center gap-2">
                <span className={`text-xs font-semibold ${cStyle.text}`}>
                  {cStyle.icon} {cStyle.label}
                </span>
                <span className="text-xs text-zinc-500 dark:text-zinc-400">
                  {label}
                </span>
                <span className="ml-auto text-xs font-mono text-zinc-400">
                  {c.target}
                </span>
              </div>
              <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300">
                {c.description}
              </p>
              {/* 数据影响详情 */}
              {c.data_impact && Object.keys(c.data_impact).length > 0 && (
                <div className="mt-2 rounded bg-black/5 p-2 text-xs dark:bg-white/5">
                  {c.data_impact.fail_count != null && (
                    <span className="mr-3">
                      采样 {c.data_impact.scanned || 0} 行，{c.data_impact.fail_count} 行无法转换
                    </span>
                  )}
                  {c.data_impact.null_count != null && (
                    <span className="mr-3">
                      NULL 值：{c.data_impact.null_count} 行
                    </span>
                  )}
                  {c.data_impact.duplicate_groups != null && (
                    <span className="mr-3">
                      重复值组：{c.data_impact.duplicate_groups} 组
                    </span>
                  )}
                  {c.data_impact.fail_samples && c.data_impact.fail_samples.length > 0 && (
                    <div className="mt-1 text-zinc-500">
                      失败样本: {c.data_impact.fail_samples.slice(0, 3).join(", ")}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* 操作按钮 */}
      <div className="mt-4 flex justify-end gap-2 border-t border-zinc-200 pt-4 dark:border-zinc-700">
        <button
          onClick={onCancel}
          className="rounded-md border border-zinc-300 px-4 py-1.5 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-600 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          取消
        </button>
        <button
          onClick={onConfirm}
          className={`rounded-md px-4 py-1.5 text-sm text-white hover:opacity-90 ${riskLevel === "danger" ? "bg-red-600 hover:bg-red-700" : "bg-amber-600 hover:bg-amber-700"}`}
        >
          确认强制执行
        </button>
      </div>
    </DraggableModal>
  );
}
