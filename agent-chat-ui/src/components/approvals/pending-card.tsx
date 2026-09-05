"use client";

import type { PendingItem } from "@/hooks/useApprovals";

/** 待审批单卡（审批交互的唯一卡片实现，20260903 从权限管理页提取）
 *
 * 消费方：权限管理页内联审批区、全局 ApprovalWatcher 弹窗——两处形态一致。
 */
export function PendingCard({
  p,
  onSettle,
}: {
  p: PendingItem;
  onSettle: (token: string, approve: boolean, name?: string) => void;
}) {
  return (
    <div className="space-y-2 rounded-lg border border-amber-200 bg-white p-3">
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm text-gray-900">
          {p.name === "__escalate__" ? "AI 提权请求" : p.name}
        </span>
        <span className="text-xs text-gray-400">
          剩余 {Math.max(0, p.ttl_remaining)}s
        </span>
      </div>
      <pre className="max-h-32 overflow-auto text-xs whitespace-pre-wrap text-gray-600">
        {p.impact}
      </pre>
      <div className="flex gap-2">
        <button
          onClick={() => onSettle(p.token, true, p.name)}
          className="rounded-lg bg-gray-900 px-3 py-1 text-xs text-white hover:bg-gray-700"
        >
          批准执行
        </button>
        <button
          onClick={() => onSettle(p.token, false, p.name)}
          className="rounded-lg border border-gray-300 px-3 py-1 text-xs text-gray-700 hover:bg-gray-50"
        >
          拒绝
        </button>
      </div>
    </div>
  );
}
