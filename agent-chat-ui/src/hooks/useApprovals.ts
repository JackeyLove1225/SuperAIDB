"use client";

import { apiFetch } from "@/lib/api-fetch";
import { useOperatorPassword } from "@/components/ui/operator-password-modal";
import { useCallback, useEffect, useState } from "react";

/** 待审批项（高危人审闸：MCP 通道挂起的高危操作） */
export interface PendingItem {
  token: string;
  name: string;
  impact: string;
  ttl_remaining: number;
  age_seconds: number;
}

/** 待审批轮询 + 结算 hook（审批交互的唯一实现，20260903 从权限管理页提取）
 *
 * 消费方：
 * - /dashboard/permissions 页面内联审批区（原 UX 不变）
 * - 全局 ApprovalWatcher（任意页面自动弹卡，MCP 同步人审桥的前端确认面）
 *
 * 多实例并存（各自 5s 轮询）是有意取舍：完全解耦、零共享状态，
 * 本地服务量级下双轮询无害。
 */
export function useApprovals(pollMs = 5000) {
  const [approvals, setApprovals] = useState<PendingItem[]>([]);
  // 高危操作第二因子：批准结算前弹操作密码框
  const { askPassword, operatorPasswordModal } = useOperatorPassword();

  const loadApprovals = useCallback(async () => {
    try {
      const d = await apiFetch<{ pending: PendingItem[] }>("/api/approvals");
      setApprovals(d.pending || []);
    } catch {
      /* 非 admin 或服务未达时静默 */
    }
  }, []);

  useEffect(() => {
    loadApprovals();
    const t = setInterval(loadApprovals, pollMs); // 挂起随时可能来
    return () => clearInterval(t);
  }, [loadApprovals, pollMs]);

  const settle = useCallback(
    async (token: string, approve: boolean, name?: string) => {
      // 批准（approve=true）属高危放行：先收集操作密码随请求发出；拒绝不需要
      let operatorPassword: string | null = null;
      if (approve) {
        operatorPassword = await askPassword();
        if (operatorPassword === null) return null; // 用户取消密码输入
      }
      try {
        // 提权请求（__escalate__）走专属结算端点——审批中心 settle 对它返回 400
        const isEscalation = name === "__escalate__";
        const url = isEscalation
          ? `/api/auth/escalations/${token}/approve?approve=${approve}`
          : `/api/approvals/${token}/settle`;
        const r = await apiFetch<{ message?: string; result?: string }>(url, {
          method: "POST",
          body: isEscalation
            ? approve
              ? JSON.stringify({ operator_password: operatorPassword })
              : undefined
            : JSON.stringify(
                approve
                  ? { approve, operator_password: operatorPassword }
                  : { approve },
              ),
        });
        return {
          ok: true as const,
          message: r.message || "已结算",
          result: r.result ? String(r.result) : "",
        };
      } catch (e) {
        return {
          ok: false as const,
          message: e instanceof Error ? e.message : "结算失败",
          result: "",
        };
      } finally {
        loadApprovals();
      }
    },
    [askPassword, loadApprovals],
  );

  return {
    approvals,
    settle,
    refresh: loadApprovals,
    askPassword,
    operatorPasswordModal,
  };
}
