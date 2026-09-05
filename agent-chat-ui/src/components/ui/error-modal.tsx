"use client";

import React, { useCallback, useEffect, useState } from "react";

/** 全局错误模态——替代 sonner toast.error 的屏幕中央错误弹窗
 *
 * 背景：右下角 toast 不显眼，"保存失败"类错误（后端报错可能很长）需要
 * 居中模态完整展示。
 *
 * 用法：
 * - 任意处调用 showError(message) 弹窗（模块级 state + 订阅，无需 Provider 包裹）
 * - <ErrorModalHost /> 在根 layout 挂一次即全局生效
 * - 样式复用 operator-password-modal 的 fixed + bg-black/50 + max-w 模式，
 *   z-index 80（高于操作密码弹窗的 70，可在其上再报 403）
 * - toast.success/warning 等非错误通知仍走 sonner <Toaster>
 */

let currentMessage: string | null = null;
const listeners = new Set<() => void>();

/** 弹出全局错误模态；重复调用时最新错误覆盖旧错误 */
// eslint-disable-next-line react-refresh/only-export-components -- 模块内维护 state，函数与组件同文件是有意设计
export function showError(message: string) {
  currentMessage = message;
  listeners.forEach((fn) => fn());
}

export function ErrorModalHost() {
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    const sync = () => setMsg(currentMessage);
    listeners.add(sync);
    return () => {
      listeners.delete(sync);
    };
  }, []);

  const close = useCallback(() => {
    currentMessage = null;
    setMsg(null);
  }, []);

  if (!msg) return null;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50"
      onClick={close}
    >
      <div
        className="w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-zinc-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-red-600 dark:text-red-400">
            ⚠️ 操作失败
          </h3>
          <button
            onClick={close}
            className="rounded p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 dark:hover:bg-zinc-800"
          >
            ✕
          </button>
        </div>
        {/* 后端报错可能很长：保留换行，超长可滚动 */}
        <div className="mb-4 max-h-[50vh] overflow-auto text-sm break-words whitespace-pre-wrap text-zinc-700 dark:text-zinc-300">
          {msg}
        </div>
        <div className="flex justify-end">
          <button
            onClick={close}
            autoFocus
            className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
          >
            知道了
          </button>
        </div>
      </div>
    </div>
  );
}
