"use client";

import { apiFetch } from "@/lib/api-fetch";
import { logout } from "@/lib/auth";
import React, { useState, useEffect } from "react";
import { Switch } from "@/components/ui/switch";
import { PasswordInput } from "@/components/ui/password-input";
import { Label } from "@/components/ui/label";
import { useOperatorPassword } from "@/components/ui/operator-password-modal";
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
      setMessage({
        type: "error",
        text: e instanceof Error ? e.message : "网络错误",
      });
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
    <div className="mx-auto h-full max-w-3xl overflow-y-auto p-6">
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
      <SettingCard
        title="开发者模式"
        desc="控制前端运行模式"
      >
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
      <SettingCard
        title="系统信息"
        desc="当前系统配置概览"
      >
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

      {/* 模块2.6：修改密码（成功后 token 全吊销，需重新登录） */}
      <ChangePasswordCard />

      {/* 模块2.7：我的收紧（用户自助加限制，只能更严） */}
      <MyRulesCard />

      {/* 模块3：行业管理 */}
      <SettingCard
        title="行业管理"
        desc="切换行业、编辑行业配置"
      >
        <IndustryManager />
      </SettingCard>

      {/* 模块4：服务控制 */}
      <SettingCard
        title="服务控制"
        desc="重启后端以应用设置变更"
      >
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
  // 高危操作第二因子：开启/关闭隔离前弹操作密码框
  const { askPassword, operatorPasswordModal } = useOperatorPassword();

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
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setBusy(true);
    setNote(null);
    try {
      const r = await apiFetch<{ message?: string }>("/api/isolation/switch", {
        method: "POST",
        body: JSON.stringify({
          enable: on,
          operator_password: operatorPassword,
        }),
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
        <Switch
          checked={active}
          onCheckedChange={toggle}
          disabled={busy}
        />
      </div>
      {note && (
        <div className="mt-3 rounded-lg bg-zinc-50 p-2 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          {note}
        </div>
      )}
      {/* 操作密码确认弹窗（高危操作第二因子） */}
      {operatorPasswordModal}
    </SettingCard>
  );
}

/** 修改密码卡片：登录用户改自己的密码（Bearer 认证由 apiFetch 自动携带）；
 * 成功后后端吊销所有已发 token（tv+1），提示后清登录态跳回登录页 */
function ChangePasswordCard() {
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  const mismatch = confirmPwd !== "" && newPwd !== confirmPwd;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPwd !== confirmPwd) {
      setNote({ type: "error", text: "两次输入的新密码不一致" });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const r = await apiFetch<{ ok?: boolean; message?: string }>(
        "/api/auth/change-password",
        {
          method: "POST",
          body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
        },
      );
      if (r && r.ok === false) throw new Error(r.message || "修改失败");
      // 改密成功所有 token 已被吊销：提示后复用 logout 清本地态并跳登录页
      setNote({ type: "success", text: "密码已修改，请重新登录" });
      setTimeout(() => logout(), 1500);
    } catch (e2) {
      setNote({
        type: "error",
        text: e2 instanceof Error ? e2.message : "修改失败",
      });
      setBusy(false);
    }
  };

  return (
    <SettingCard
      title="修改密码"
      desc="修改当前登录账号的密码；修改成功后需要重新登录"
    >
      <form
        onSubmit={submit}
        className="space-y-3"
      >
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cp-old">当前密码</Label>
            <PasswordInput
              id="cp-old"
              value={oldPwd}
              onChange={(e) => setOldPwd(e.target.value)}
              required
              autoComplete="current-password"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cp-new">新密码</Label>
            <PasswordInput
              id="cp-new"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
              required
              minLength={6}
              autoComplete="new-password"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cp-confirm">确认新密码</Label>
            <PasswordInput
              id="cp-confirm"
              value={confirmPwd}
              onChange={(e) => setConfirmPwd(e.target.value)}
              required
              minLength={6}
              autoComplete="new-password"
            />
          </div>
        </div>
        {mismatch && (
          <p className="text-xs text-red-600 dark:text-red-400">
            两次输入的新密码不一致
          </p>
        )}
        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={busy || mismatch || !oldPwd || !newPwd || !confirmPwd}
            className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {busy ? "提交中…" : "修改密码"}
          </button>
        </div>
        {note && (
          <div
            className={`rounded-lg p-2 text-xs ${
              note.type === "success"
                ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400"
                : "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400"
            }`}
          >
            {note.text}
          </div>
        )}
      </form>
    </SettingCard>
  );
}

/** 我的收紧卡片：用户自助加限制（PUT /api/auth/my-rules，deny-only 契约——
 * 只收 deny 键，含 allow/mode 会被后端 400；空 rules={} = 清除全部）。
 * 列级自助不提供（字段级收紧请联系管理员）。 */
const SELF_OPS = [
  ["query", "查询"],
  ["insert", "插入"],
  ["update", "修改"],
  ["delete", "删除"],
  ["ddl", "建改结构"],
  ["drop", "删除结构"],
] as const;
const SELF_OP_LABEL: Record<string, string> = Object.fromEntries(SELF_OPS);

interface MyRulesDoc {
  deny?: string[];
  tables?: Record<string, { deny?: string[] }>;
}

function MyRulesCard() {
  const [loading, setLoading] = useState(true);
  const [adminRules, setAdminRules] = useState<MyRulesDoc | null>(null);
  const [opDenied, setOpDenied] = useState<Set<string>>(new Set());
  const [tableRules, setTableRules] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const [newTable, setNewTable] = useState("");
  const [newTableOps, setNewTableOps] = useState<Set<string>>(new Set());

  const load = React.useCallback(() => {
    apiFetch<{ admin_rules?: MyRulesDoc; self_rules?: MyRulesDoc }>(
      "/api/auth/my-rules",
    )
      .then((d) => {
        setAdminRules(
          d.admin_rules && Object.keys(d.admin_rules).length > 0
            ? d.admin_rules
            : null,
        );
        const sr = d.self_rules || {};
        setOpDenied(new Set(sr.deny || []));
        const tm: Record<string, string[]> = {};
        for (const [t, ts] of Object.entries(sr.tables || {})) {
          if (ts?.deny && ts.deny.length > 0) tm[t] = ts.deny;
        }
        setTableRules(tm);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const save = async (rules: MyRulesDoc | Record<string, never>) => {
    setBusy(true);
    setNote(null);
    try {
      await apiFetch("/api/auth/my-rules", {
        method: "PUT",
        body: JSON.stringify({ rules }),
      });
      setNote({ type: "success", text: "已保存并生效" });
      load();
    } catch (e) {
      setNote({
        type: "error",
        text: e instanceof Error ? e.message : "保存失败",
      });
    } finally {
      setBusy(false);
    }
  };

  // 保存：组装 deny-only 规则文档（空键不下发）
  const saveAll = () => {
    const rules: MyRulesDoc = {};
    if (opDenied.size > 0) rules.deny = [...opDenied];
    const tm = Object.fromEntries(
      Object.entries(tableRules)
        .filter(([, ops]) => ops.length > 0)
        .map(([t, ops]) => [t, { deny: ops }]),
    );
    if (Object.keys(tm).length > 0) rules.tables = tm;
    save(rules);
  };

  const addTable = () => {
    const t = newTable.trim();
    if (!t || newTableOps.size === 0) return;
    setTableRules((m) => ({ ...m, [t]: [...newTableOps] }));
    setNewTable("");
    setNewTableOps(new Set());
  };

  const toggleOp =
    (set: Set<string>, apply: (s: Set<string>) => void) => (op: string) => {
      const s = new Set(set);
      if (s.has(op)) s.delete(op);
      else s.add(op);
      apply(s);
    };

  return (
    <SettingCard
      title="我的收紧"
      desc="给自己加限制，只能比管理员给的更严。管理员授予的限制（如有）显示在下方，不可在此解除。字段级收紧请联系管理员。"
    >
      {loading ? (
        <p className="text-sm text-zinc-400">加载中…</p>
      ) : (
        <div className="space-y-4">
          {/* 管理员授予的限制（只读） */}
          {adminRules && (
            <div>
              <p className="mb-1 text-xs text-zinc-500 dark:text-zinc-400">
                管理员授予的限制（只读）
              </p>
              <pre className="max-h-40 overflow-auto rounded-lg bg-zinc-50 p-2 text-xs text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                {JSON.stringify(adminRules, null, 2)}
              </pre>
            </div>
          )}

          {/* 操作级自我禁止 */}
          <div>
            <p className="mb-2 text-sm text-zinc-700 dark:text-zinc-300">
              操作级（对我的一切数据源生效）
            </p>
            <div className="flex flex-wrap gap-4">
              {SELF_OPS.map(([op, label]) => (
                <label
                  key={op}
                  className="flex items-center gap-1 text-sm text-zinc-700 dark:text-zinc-300"
                >
                  <input
                    type="checkbox"
                    className="accent-zinc-900"
                    checked={opDenied.has(op)}
                    onChange={() => toggleOp(opDenied, setOpDenied)(op)}
                  />
                  禁止{label}
                </label>
              ))}
            </div>
          </div>

          {/* 表级收紧 */}
          <div>
            <p className="mb-2 text-sm text-zinc-700 dark:text-zinc-300">
              表级收紧
            </p>
            {Object.keys(tableRules).length === 0 ? (
              <p className="text-xs text-zinc-400">暂无表级收紧</p>
            ) : (
              <div className="mb-2 space-y-1">
                {Object.entries(tableRules).map(([t, ops]) => (
                  <div
                    key={t}
                    className="flex items-center gap-2 text-sm"
                  >
                    <span className="font-mono text-zinc-900 dark:text-zinc-100">
                      {t}
                    </span>
                    <span className="text-xs text-zinc-500">
                      禁止：{ops.map((o) => SELF_OP_LABEL[o] ?? o).join("、")}
                    </span>
                    <button
                      className="text-xs text-rose-600 hover:underline"
                      onClick={() =>
                        setTableRules((m) => {
                          const n = { ...m };
                          delete n[t];
                          return n;
                        })
                      }
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            )}
            {/* 添加表级收紧 */}
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-200 p-2 dark:border-zinc-700">
              <input
                className="w-40 rounded-lg border border-zinc-300 px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-800"
                placeholder="表名"
                value={newTable}
                onChange={(e) => setNewTable(e.target.value)}
              />
              {SELF_OPS.map(([op, label]) => (
                <label
                  key={op}
                  className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400"
                >
                  <input
                    type="checkbox"
                    className="accent-zinc-900"
                    checked={newTableOps.has(op)}
                    onChange={() => toggleOp(newTableOps, setNewTableOps)(op)}
                  />
                  {label}
                </label>
              ))}
              <button
                onClick={addTable}
                disabled={!newTable.trim() || newTableOps.size === 0}
                className="rounded-lg border border-zinc-300 px-3 py-1 text-xs text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
              >
                添加
              </button>
            </div>
          </div>

          {/* 操作按钮 */}
          <div className="flex items-center gap-3">
            <button
              onClick={saveAll}
              disabled={busy}
              className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
            >
              {busy ? "保存中…" : "保存收紧"}
            </button>
            <button
              onClick={() => save({})}
              disabled={busy}
              className="rounded-lg border border-zinc-300 px-4 py-2 text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              全部清除
            </button>
          </div>

          {note && (
            <div
              className={`rounded-lg p-2 text-xs ${
                note.type === "success"
                  ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400"
                  : "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400"
              }`}
            >
              {note.text}
            </div>
          )}
        </div>
      )}
    </SettingCard>
  );
}
