"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Table2,
  Database,
  Settings,
  ShieldCheck,
  Users,
  LogOut,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchMe, getCachedUser, logout, type AuthUser } from "@/lib/auth";

/**
 * 展示板侧栏（20260808）：对话功能移除后，仅保留管理/展示导航。
 * 聊天专属 UI（新聊天、最近/文件 tab、会话历史）已删除。
 */
const SIDEBAR_MIN = 200;
const SIDEBAR_MAX = 320;
const SIDEBAR_KEY = "superaidb_sidebar_w";

export function AppSidebar() {
  const pathname = usePathname();

  const bottomLinks = [
    { href: "/dashboard", label: "控制台", icon: LayoutDashboard, active: pathname === "/dashboard" },
    { href: "/dashboard/schema-designer", label: "表设计", icon: Table2, active: pathname.startsWith("/dashboard/schema-designer") },
    { href: "/dashboard/datasources", label: "数据源", icon: Database, active: pathname === "/dashboard/datasources" },
    { href: "/dashboard/permissions", label: "权限", icon: ShieldCheck, active: pathname === "/dashboard/permissions" },
    { href: "/settings", label: "设置", icon: Settings, active: pathname === "/settings" },
  ];

  // 右缘拖拽调宽（持久化 localStorage）
  const [width, setWidth] = useState(220);
  const asideRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const saved = Number(localStorage.getItem(SIDEBAR_KEY));
    if (saved >= SIDEBAR_MIN && saved <= SIDEBAR_MAX) setWidth(saved);
  }, []);
  const startDrag = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = asideRef.current?.offsetWidth ?? 220;
    const onMove = (ev: MouseEvent) => {
      const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, startW + ev.clientX - startX));
      setWidth(w);
    };
    const onUp = (ev: MouseEvent) => {
      const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, startW + ev.clientX - startX));
      setWidth(w);
      localStorage.setItem(SIDEBAR_KEY, String(w));
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  // 当前登录用户（缓存先行，fetchMe 后台校准；system 模式不显示用户区）
  const [me, setMe] = useState<AuthUser | null>(() => getCachedUser());
  useEffect(() => {
    let cancelled = false;
    fetchMe()
      .then((u) => { if (!cancelled) setMe(u); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);
  const isSystemMode = !me || me.role === "system";
  if (me?.role === "admin") {
    bottomLinks.splice(4, 0, {
      href: "/dashboard/users", label: "用户管理", icon: Users,
      active: pathname === "/dashboard/users",
    });
  }

  // 登录页不渲染侧边栏（全屏登录表单）
  if (pathname === "/login") return null;

  return (
    <aside
      ref={asideRef}
      style={{ width }}
      className="relative flex h-full shrink-0 flex-col border-r border-[#ececec] bg-[#f9f9f9]"
    >
      {/* 右缘拖拽手柄 */}
      <div
        onMouseDown={startDrag}
        title="拖动调整侧栏宽度"
        className="absolute inset-y-0 right-[-3px] z-20 w-[6px] cursor-col-resize transition-colors hover:bg-[#d0d0d0]/60"
      />
      {/* 品牌 */}
      <div className="flex items-center gap-2 px-5 pb-3 pt-4">
        <span className="flex h-6 w-6 items-center justify-center rounded-[7px] bg-[#0d0d0d] text-[13px] font-medium text-white">
          S
        </span>
        <span className="text-[15px] font-medium tracking-tight text-[#0d0d0d]">SuperAIDB</span>
      </div>

      {/* 底部导航 */}
      <div className="mt-auto border-t border-[#ececec] p-2">
        {bottomLinks.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className={cn(
              "flex items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors",
              link.active
                ? "bg-[#efefef] font-medium text-[#0d0d0d]"
                : "text-[#6e6e6e] hover:bg-[#efefef] hover:text-[#0d0d0d]",
            )}
          >
            <link.icon className="size-4" strokeWidth={1.75} />
            {link.label}
          </Link>
        ))}
        {/* 当前用户 + 退出（系统模式不显示） */}
        {!isSystemMode && me && (
          <div className="mt-1 flex items-center gap-2 rounded-lg border-t border-[#ececec] px-3 pt-2 pb-1">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-[11px] font-medium text-white">
              {me.username.slice(0, 1).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1 truncate text-[13px] text-[#0d0d0d]" title={me.username}>
              {me.username}
              <span className="ml-1 text-[11px] text-[#9e9e9e]">
                {me.role === "admin" ? "管理员" : me.role === "readonly" ? "只读" : ""}
              </span>
            </span>
            <button
              type="button"
              onClick={logout}
              title="退出登录"
              className="text-[#9e9e9e] transition-colors hover:text-[#0d0d0d]"
            >
              <LogOut className="size-4" strokeWidth={1.75} />
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
