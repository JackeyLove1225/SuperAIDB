"use client";

import React, { useCallback, useRef, useState } from "react";
import { PasswordInput } from "@/components/ui/password-input";

/** 操作密码确认（高危/写操作第二因子）
 *
 * 后端对一批高危端点要求 JSON body 带 operator_password 字段，
 * 否则 403「操作密码错误或未提供」。本 Hook 提供统一的密码收集弹窗：
 * - askPassword() 弹出密码框，resolve 用户输入；取消 resolve null（调用方中断操作）
 * - 返回的 operatorPasswordModal 需渲染在触发页面中（fixed 定位，挂哪都行）
 * - 弹窗只负责收集密码；后端 403 错误由调用方既有错误链路呈现
 *   （apiFetch 已提取 detail 消息，toast/行内错误提示均可见）
 * - 样式复用表格页删除确认弹窗的 fixed + bg-black/50 + max-w-md 模式，
 *   z-index 提到 70 以叠在删除确认等 z-50 弹窗之上
 */
export function useOperatorPassword() {
  const [open, setOpen] = useState(false);
  const [pwd, setPwd] = useState("");
  const resolverRef = useRef<((v: string | null) => void) | null>(null);

  const askPassword = useCallback((): Promise<string | null> => {
    setPwd("");
    setOpen(true);
    return new Promise<string | null>((resolve) => {
      resolverRef.current = resolve;
    });
  }, []);

  const finish = useCallback((v: string | null) => {
    setOpen(false);
    resolverRef.current?.(v);
    resolverRef.current = null;
  }, []);

  const operatorPasswordModal = open ? (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50"
      onClick={() => finish(null)}
    >
      <form
        className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-zinc-900"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault();
          if (pwd) finish(pwd);
        }}
      >
        <h3 className="mb-2 text-lg font-semibold text-zinc-900 dark:text-zinc-100">
          🔒 高危操作确认
        </h3>
        <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
          请输入<b>当前登录账户</b>的密码（谁的会话谁确认）
        </p>
        <PasswordInput
          autoFocus
          value={pwd}
          onChange={(e) => setPwd(e.target.value)}
          placeholder="当前账户密码"
          autoComplete="off"
          className="mb-4"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => finish(null)}
            className="rounded-md border border-zinc-300 px-4 py-2 text-sm text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={!pwd}
            className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            确认
          </button>
        </div>
      </form>
    </div>
  ) : null;

  return { askPassword, operatorPasswordModal };
}
