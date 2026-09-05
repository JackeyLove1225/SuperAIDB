import type { Metadata } from "next";
import "./globals.css";
import { Inter } from "next/font/google";
import React from "react";
import { AppSidebar } from "@/components/app-sidebar";
import { StartupOverlay } from "@/components/StartupOverlay";
import { AuthGuard } from "@/components/auth-guard";
import { Toaster } from "@/components/ui/sonner";
import { ErrorModalHost } from "@/components/ui/error-modal";
import { ApprovalWatcher } from "@/components/approvals/approval-watcher";

const inter = Inter({
  subsets: ["latin"],
  preload: true,
  display: "swap",
});

export const metadata: Metadata = {
  title: "SuperAIDB",
  description: "SuperAIDB 管理控制台",
};

/**
 * 展示板模式（20260808）：对话 Provider 树已移除，layout 仅保留
 * 启动检查（:2025）、认证守卫、侧栏与全局 toast。
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className={inter.className}>
        <StartupOverlay />
        <AuthGuard>
          <div className="flex h-screen overflow-hidden">
            <AppSidebar />
            <main className="min-w-0 flex-1 overflow-hidden bg-white">
              {children}
            </main>
            <Toaster />
            {/* 全局错误模态：showError() 的渲染点（z-80，高于密码弹窗） */}
            <ErrorModalHost />
            {/* 全局待审批监听：MCP 高危挂起任意页面自动弹卡（z-60） */}
            <ApprovalWatcher />
          </div>
        </AuthGuard>
      </body>
    </html>
  );
}
