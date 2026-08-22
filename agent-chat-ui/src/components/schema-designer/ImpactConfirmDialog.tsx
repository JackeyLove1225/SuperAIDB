"use client";

// 删除影响面确认弹窗——删表/删外键边共用（替代浏览器原生 confirm）
// 数据源为后端 precheck 报告：打开即预检，预检失败 fail-closed 仅可取消，
// 与 TableEditorModal 的 precheck 失败中止保存同一口径。
import { Loader2, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";

export interface ImpactSection {
  label: string;
  /** 每行一条影响面描述；以 ⚠ 开头的行用警示色渲染 */
  lines: string[];
}

interface ImpactConfirmDialogProps {
  title: string;
  /** 预检加载中 */
  loading: boolean;
  /** 预检失败（fail-closed：仅可取消，不允许盲点确认删除） */
  error?: string | null;
  /** 确认执行中（防重复点击） */
  busy?: boolean;
  sections: ImpactSection[];
  warning?: string;
  confirmText?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ImpactConfirmDialog({
  title,
  loading,
  error,
  busy,
  sections,
  warning,
  confirmText = "确认删除",
  onConfirm,
  onCancel,
}: ImpactConfirmDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-[2px]">
      <div className="flex max-h-[80vh] w-[560px] flex-col overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-2xl dark:border-zinc-700 dark:bg-zinc-900">
        <div className="border-b border-zinc-100 px-5 py-4 dark:border-zinc-800">
          <h3 className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            {title}
          </h3>
        </div>

        <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4 text-sm">
          {loading && (
            <div className="flex items-center gap-2 text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" />
              正在统计影响面…
            </div>
          )}
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
              影响面预检失败：{error}
              <div className="mt-1 text-xs opacity-80">
                为安全起见本次删除已中止，请刷新后重试。
              </div>
            </div>
          )}
          {!loading && !error && (
            <>
              {sections.map((sec) => (
                <div key={sec.label} className="space-y-1">
                  <div className="text-xs font-medium tracking-wide text-zinc-500">
                    {sec.label}
                  </div>
                  <div className="space-y-0.5 rounded-lg bg-zinc-50 px-3 py-2 font-mono text-[13px] leading-5 dark:bg-zinc-800/60">
                    {sec.lines.map((line, i) => (
                      <div
                        key={i}
                        className={
                          line.startsWith("⚠")
                            ? "text-amber-600 dark:text-amber-400"
                            : "text-zinc-800 dark:text-zinc-200"
                        }
                      >
                        {line}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {warning && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
                  <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{warning}</span>
                </div>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-zinc-100 px-5 py-3 dark:border-zinc-800">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          {!error && (
            <Button
              onClick={onConfirm}
              disabled={loading || busy}
              className="bg-red-600 text-white hover:bg-red-700"
            >
              {busy ? "删除中…" : confirmText}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
