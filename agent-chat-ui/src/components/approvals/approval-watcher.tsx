"use client";

import { useApprovals } from "@/hooks/useApprovals";
import { PendingCard } from "@/components/approvals/pending-card";
import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { toast } from "sonner";

/** 全局待审批监听器（MCP 同步人审桥的前端确认面，20260903）
 *
 * 挂载于根布局（AuthGuard 内）——任意页面检测到新的高危挂起即自动弹卡，
 * 用户不必离开当前页去找审批中心（体验对齐前端"操作现场弹窗"）。
 * 结算逻辑完全复用 useApprovals（与权限管理页同一实现，零复制）。
 *
 * 交互细节：
 * - 已在 /dashboard/permissions 时不弹（页面自带审批区，避免双卡）
 * - "稍后处理"记住 dismissed token（不再自动弹；权限管理页仍可见）
 * - 页面在后台（document.hidden）时发浏览器通知——只在已授权
 *   （permission === "granted"）时发；不做无手势的 requestPermission
 *   （Chrome 要求 user gesture，无手势必挂起，未授权用户靠页面弹窗）
 */
export function ApprovalWatcher() {
  const { approvals, settle, refresh, operatorPasswordModal } = useApprovals();
  const pathname = usePathname();
  // seen：见过的 token（跨轮询去重，只对"新到的"弹窗）
  const seen = useRef<Set<string>>(new Set());
  const dismissed = useRef<Set<string>>(new Set());
  const [open, setOpen] = useState(false);

  const onPermissionsPage = pathname === "/dashboard/permissions";

  useEffect(() => {
    if (approvals.length === 0) return;
    // 标记本轮全部 token 为已见（首挂载时存量挂起不弹窗，只对增量弹）
    const fresh = approvals.filter((p) => !seen.current.has(p.token));
    approvals.forEach((p) => seen.current.add(p.token));
    if (fresh.length === 0) return;
    const unhandled = fresh.filter((p) => !dismissed.current.has(p.token));
    if (unhandled.length === 0) return;
    if (!onPermissionsPage) {
      setOpen(true);
    }
    // 后台标签页：浏览器通知（已授权才发；点击通知聚焦窗口）
    if (typeof document !== "undefined" && document.hidden) {
      try {
        if (typeof Notification !== "undefined"
            && Notification.permission === "granted") {
          const first = unhandled[0];
          const n = new Notification("SuperAIDB 高危操作待审批", {
            body: `${first.name === "__escalate__" ? "AI 提权请求" : first.name}（${unhandled.length} 项待处理）`,
            tag: "superaidb-approvals",
          });
          n.onclick = () => {
            window.focus();
            setOpen(true);
          };
        }
      } catch {
        /* 通知 API 不可用（旧浏览器/http 环境）——静默，页面弹窗兜底 */
      }
    }
  }, [approvals, onPermissionsPage]);

  // 弹窗展示的条目：未 dismissed 的现存挂起（随轮询刷新 TTL/增删）
  const items = approvals.filter((p) => !dismissed.current.has(p.token));
  // 已全部处理完（或过期消失）→ 自动关窗
  useEffect(() => {
    if (open && items.length === 0) setOpen(false);
  }, [open, items.length]);

  if (!open || items.length === 0) {
    return <>{operatorPasswordModal}</>;
  }

  return (
    <>
      <div
        className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50"
        onClick={() => setOpen(false)}
      >
        <div
          className="max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-lg bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-1 text-lg font-semibold text-gray-900">
            ⏸️ 高危操作待审批（{items.length}）
          </div>
          <p className="mb-4 text-sm text-gray-500">
            AI 客户端发起的高危操作正在等待你的批准——批准需输入操作密码，
            拒绝则操作不执行。处理完成后结果会自动返回 AI 对话。
          </p>
          <div className="space-y-3">
            {items.map((p) => (
              <PendingCard
                key={p.token}
                p={p}
                onSettle={async (token, approve, name) => {
                  const r = await settle(token, approve, name);
                  if (r === null) return; // 用户取消密码输入——卡片保留可重试
                  if (r.ok) {
                    dismissed.current.add(token); // 结算完成——移出弹窗
                  } else {
                    // 结算失败（如密码错误）：卡片保留重试，toast 呈现原因
                    toast.error(r.message || "结算失败");
                  }
                  refresh();
                }}
              />
            ))}
          </div>
          <div className="mt-4 flex justify-end">
            <button
              onClick={() => {
                items.forEach((p) => dismissed.current.add(p.token));
                setOpen(false);
              }}
              className="rounded-md border border-zinc-300 px-4 py-2 text-sm text-zinc-700 hover:bg-zinc-100"
            >
              稍后处理
            </button>
          </div>
        </div>
      </div>
      {operatorPasswordModal}
    </>
  );
}
