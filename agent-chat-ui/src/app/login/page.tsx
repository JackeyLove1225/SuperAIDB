"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PasswordInput } from "@/components/ui/password-input";
import { Label } from "@/components/ui/label";
import { login, register } from "@/lib/auth";

/** 登录/注册页（迭代 1.5）
 *
 * - 登录成功：token 持久化 localStorage，跳转首页
 * - 注册：公开注册角色固定 user（admin 由用户管理页创建）
 * - 登录/注册失败的 401 不触发全局跳转（apiFetch skipUnauthorizedHandler）
 */
export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (mode === "register") {
      if (password !== confirm) {
        setError("两次输入的密码不一致");
        return;
      }
    }
    setSubmitting(true);
    try {
      if (mode === "login") {
        await login(username.trim(), password);
      } else {
        await register(username.trim(), password);
        await login(username.trim(), password); // 注册后直接登录
      }
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-gray-50 p-4">
      <div className="bg-background flex w-full max-w-sm flex-col rounded-lg border shadow-lg">
        <div className="mt-10 flex flex-col gap-1 border-b p-6">
          <h1 className="text-xl font-semibold tracking-tight">SuperAIDB</h1>
          <p className="text-muted-foreground text-sm">
            {mode === "login" ? "登录你的账号" : "注册新账号（普通用户角色）"}
          </p>
        </div>
        <form onSubmit={onSubmit} className="flex flex-col gap-4 p-6">
          <div className="flex flex-col gap-2">
            <Label htmlFor="username">用户名</Label>
            <Input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
              minLength={2}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="password">密码</Label>
            <PasswordInput
              id="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
              minLength={6}
            />
          </div>
          {mode === "register" && (
            <div className="flex flex-col gap-2">
              <Label htmlFor="confirm">确认密码</Label>
              <PasswordInput
                id="confirm"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
                minLength={6}
              />
            </div>
          )}
          {error && <p className="text-sm text-rose-600">{error}</p>}
          <Button type="submit" size="lg" disabled={submitting}>
            {submitting ? "请稍候…" : mode === "login" ? "登录" : "注册并登录"}
          </Button>
          <button
            type="button"
            className="text-muted-foreground text-sm underline-offset-2 hover:underline"
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError(null);
            }}
          >
            {mode === "login" ? "没有账号？注册" : "已有账号？登录"}
          </button>
        </form>
      </div>
    </div>
  );
}
