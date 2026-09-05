"use client";

import React, { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { fetchMe } from "@/lib/auth";

/** 路由守护（迭代 1.5）
 *
 * 判定逻辑：
 * - /login 路径直接放行
 * - 调 /api/auth/me：
 *   - 200 且 role=system → 认证未启用（API_KEY_ENABLED=false），放行
 *   - 200 且真实用户 → 已登录，放行
 *   - null（401）→ 未登录，跳 /login
 * - 请求异常（后端未起等）：放行——StartupOverlay 负责后端就绪轮询，
 *   守护不掺和可用性问题，避免与启动流程打架
 * - fetchMe 返回前（含持有 token 待验证）一律不渲染受保护内容，
 *   避免无效/过期 token 下受控页面先闪现再跳登录
 */
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (pathname === "/login") {
      setChecked(true);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const me = await fetchMe();
        if (cancelled) return;
        if (!me) {
          // 认证启用但未登录（或无有效 token）
          router.replace("/login");
          return;
        }
        setChecked(true);
      } catch {
        // 后端不可达等异常：放行（启动期 StartupOverlay 接管）
        if (!cancelled) setChecked(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, router]);

  if (pathname === "/login") return <>{children}</>;
  // 用户信息未验证完前渲染空（加载态）：无 token 时等 me 判定系统模式，
  // 有 token 时也必须等 me 验证通过，不能先渲染受保护页
  if (!checked) return null;
  return <>{children}</>;
}
