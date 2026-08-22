"use client";

import { apiFetch } from "@/lib/api-fetch";
import React, { useState, useEffect } from "react";
import { Switch } from "@/components/ui/switch";
import IndustryManager from "@/components/IndustryManager";

// 通过 Next.js 服务端代理访问 Management API（密钥不出服务器）


interface Settings {
  frontend_dev_mode: boolean;
  industry: string;
  ai_model: string;
}

type MessageType = "success" | "error" | "warning";

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{
    type: MessageType;
    text: string;
  } | null>(null);

  // 加载设置
  useEffect(() => {
    apiFetch<Settings>("/api/settings")
      .then((data) => {
        setSettings(data);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
        setMessage({ type: "error", text: "无法连接后端服务" });
      });
  }, []);

  // 切换开发者模式
  const toggleDevMode = async (enabled: boolean) => {
    if (!settings) return;
    setSaving(true);
    setMessage(null);
    try {
      await apiFetch("/api/settings", {
        method: "POST",
        body: JSON.stringify({ frontend_dev_mode: enabled }),
      });
      setSettings({ ...settings, frontend_dev_mode: enabled });
      setMessage({
        type: "warning",
        text: "设置已保存，需要重启后端生效",
      });
    } catch (e) {
      setMessage({ type: "error", text: e instanceof Error ? e.message : "网络错误" });
    } finally {
      setSaving(false);
    }
  };

  // 重启服务（调用 stop 接口，用户需手动重新启动）
  const restartServices = async () => {
    if (
      !window.confirm(
        "确认停止所有服务？停止后需要手动重新启动（双击桌面快捷方式）。",
      )
    )
      return;
    setMessage({ type: "warning", text: "正在停止服务，请稍后重新启动..." });
    try {
      await apiFetch("/api/stop", { method: "POST" });
    } catch {
      // 忽略——服务停止会导致连接中断
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-[calc(100vh-49px)] items-center justify-center">
        <p className="text-zinc-500">加载中...</p>
      </div>
    );
  }

  const messageStyles: Record<MessageType, string> = {
    success:
      "border-green-200 bg-green-50 text-green-800 dark:border-green-800 dark:bg-green-950 dark:text-green-400",
    warning:
      "border-yellow-200 bg-yellow-50 text-yellow-800 dark:border-yellow-800 dark:bg-yellow-950 dark:text-yellow-400",
    error:
      "border-red-200 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-400",
  };

  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="mb-6 text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
        设置
      </h1>

      {/* 消息提示 */}
      {message && (
        <div
          className={`mb-4 rounded-md border p-3 text-sm ${messageStyles[message.type]}`}
        >
          {message.text}
        </div>
      )}

      {/* 模块1：开发者模式 */}
      <SettingCard title="开发者模式" desc="控制前端运行模式">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1">
            <p className="text-sm text-zinc-700 dark:text-zinc-300">
              前端开发模式
            </p>
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              {settings?.frontend_dev_mode
                ? "已开启：使用 next dev，支持热更新，启动较慢（~30-70秒）"
                : "已关闭：使用 next start，启动快（~5秒），需先运行 build_frontend.bat 构建生产版本"}
            </p>
          </div>
          <Switch
            checked={settings?.frontend_dev_mode ?? false}
            onCheckedChange={toggleDevMode}
            disabled={saving}
          />
        </div>
        {settings?.frontend_dev_mode && (
          <div className="mt-3 rounded-lg bg-zinc-50 p-2 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
            提示：开发模式适用于修改前端代码时使用，日常使用建议关闭以获得更快的启动速度。
          </div>
        )}
      </SettingCard>

      {/* 模块2：系统信息（只读） */}
      <SettingCard title="系统信息" desc="当前系统配置概览">
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-zinc-500">行业模式</dt>
            <dd className="font-medium text-zinc-900 dark:text-zinc-100">
              {settings?.industry || "-"}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-zinc-500">AI 模型</dt>
            <dd className="font-medium text-zinc-900 dark:text-zinc-100">
              {settings?.ai_model || "-"}
            </dd>
          </div>
        </dl>
      </SettingCard>

      {/* 模块2.5：系统级数据隔离（三期产品化：一次性 UAC 授权后全自动） */}
      <IsolationCard />

      {/* 模块3：行业管理 */}
      <SettingCard title="行业管理" desc="切换行业、编辑行业配置">
        <IndustryManager />
      </SettingCard>

      {/* 模块4：服务控制 */}
      <SettingCard title="服务控制" desc="重启后端以应用设置变更">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          修改设置后，需要重启后端服务才能生效。点击下方按钮将停止所有服务，
          随后请双击桌面快捷方式重新启动。
        </p>
        <button
          onClick={restartServices}
          className="mt-3 rounded-lg border border-red-600/40 bg-white px-4 py-2 text-sm font-medium text-red-600 transition-colors hover:bg-red-50 disabled:opacity-50 dark:bg-transparent dark:hover:bg-red-950"
          disabled={saving}
        >
          停止并重启服务
        </button>
      </SettingCard>

      {/* 预留：更多设置模块 */}
      <p className="mt-6 text-center text-xs text-zinc-400">
        更多设置选项即将推出
      </p>
    </div>
  );
}

/** 通用设置卡片组件——后续新增设置项可复用 */
function SettingCard({
  title,
  desc,
  children,
}: {
  title: string;
  desc: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-4 rounded-[16px] border border-[#ececec] bg-white p-5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] dark:border-zinc-800 dark:bg-zinc-900">
      <div className="mb-3">
        <h2 className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          {title}
        </h2>
        <p className="text-xs text-zinc-500">{desc}</p>
      </div>
      {children}
    </div>
  );
}

/** 系统级数据隔离卡片：daemon 切到专用服务账号 + 数据目录 ACL 收紧（一次性 UAC 授权） */
function IsolationCard() {
  const [active, setActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = React.useCallback(() => {
    apiFetch<{ active?: boolean; daemon_as_service?: boolean }>(
      "/api/isolation/status",
    )
      .then((d) => setActive(!!d.active))
      .catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000); // 状态轮询（UAC 授权是异步的）
    return () => clearInterval(t);
  }, [refresh]);

  const toggle = async (on: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      const r = await apiFetch<{ message?: string }>("/api/isolation/switch", {
        method: "POST",
        body: JSON.stringify({ enable: on }),
      });
      setNote(
        r.message ||
          (on
            ? "已请求系统授权（UAC），请在弹窗点「是」"
            : "已请求系统授权（UAC）以关闭隔离"),
      );
      // UAC 授权后脚本是异步的：等几秒再刷新状态
      setTimeout(refresh, 5000);
      setTimeout(refresh, 12000);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingCard
      title="系统级数据隔离"
      desc="数据守护进程切到专用服务账号 + 数据目录 ACL 收紧（加密之上的第二层保险）"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            {active ? "已隔离（服务账号运行）" : "未隔离（当前用户运行）"}
          </p>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            开启后其他 OS 账号物理读不到数据文件；开启/关闭时 Windows 会弹一次
            UAC 授权框，点「是」即全自动完成
          </p>
        </div>
        <Switch checked={active} onCheckedChange={toggle} disabled={busy} />
      </div>
      {note && (
        <div className="mt-3 rounded-lg bg-zinc-50 p-2 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          {note}
        </div>
      )}
    </SettingCard>
  );
}
