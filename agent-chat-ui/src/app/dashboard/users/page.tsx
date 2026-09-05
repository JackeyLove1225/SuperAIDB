"use client";

import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api-fetch";
import { getCachedUser } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PasswordInput } from "@/components/ui/password-input";
import { useOperatorPassword } from "@/components/ui/operator-password-modal";
import { Label } from "@/components/ui/label";

/** 用户管理页（迭代 1.5，admin 限定）
 *
 * - 列表：GET /api/auth/users
 * - 创建：POST /api/auth/users（可指定角色，admin 专属端点）
 * - 删除：DELETE /api/auth/users/{id}（默认 admin 不可删）
 * - 改角色：PUT /api/auth/users/{id}/role（默认 admin 与本人锁定）
 * 三类写操作均带操作密码第二因子（askPassword → operator_password）。
 * 非 admin 访问时后端 403，页面如实呈现错误。
 */

interface UserRow {
  id: number;
  username: string;
  role: string;
  created_at: string;
  last_login: string | null;
}

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员",
  user: "普通用户",
  readonly: "只读用户",
};

const BUILTIN_ROLES = ["admin", "user", "readonly"];

/** 角色单元格：可变更行渲染下拉框（变更需操作密码确认），锁定行渲染静态标签。
 * 受控 select + 本地状态：密码取消/失败时回滚显示值，成功后随刷新同步。 */
function RoleCell({
  u,
  me,
  onRoleChange,
}: {
  u: UserRow;
  me: { user_id?: number } | null;
  onRoleChange: (u: UserRow, role: string) => Promise<boolean>;
}) {
  const [val, setVal] = useState(u.role);
  // 外部数据刷新后（成功变更/他人改动）同步显示值
  useEffect(() => setVal(u.role), [u.role]);
  // 默认 admin 与本人角色锁定（后端同口径拒绝，前端直接不给入口）
  if (u.username === "admin" || u.id === me?.user_id) {
    return <span>{ROLE_LABEL[u.role] ?? u.role}</span>;
  }
  return (
    <select
      value={val}
      onChange={async (e) => {
        const next = e.target.value;
        if (next === u.role) return;
        setVal(next); // 即时反馈选中值
        if (!(await onRoleChange(u, next))) setVal(u.role); // 取消/失败回滚
      }}
      className="border-input bg-background h-8 rounded-md border px-2 text-sm"
      aria-label={`修改用户 ${u.username} 的角色`}
    >
      <option value="user">普通用户</option>
      <option value="readonly">只读用户</option>
      <option value="admin">管理员</option>
      {/* 自定义角色：保留当前值可选项（后端 _validate_role 放行自定义角色名） */}
      {!BUILTIN_ROLES.includes(u.role) && (
        <option value={u.role}>{ROLE_LABEL[u.role] ?? u.role}</option>
      )}
    </select>
  );
}

export default function UsersPage() {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const [newName, setNewName] = useState("");
  const [newPass, setNewPass] = useState("");
  const [newRole, setNewRole] = useState("user");
  const [creating, setCreating] = useState(false);
  // 高危操作第二因子：创建/删除用户前弹操作密码框
  const { askPassword, operatorPasswordModal } = useOperatorPassword();

  const me = getCachedUser();

  const load = useCallback(async () => {
    setErr(null);
    try {
      const data = await apiFetch<{ users: UserRow[] }>("/api/auth/users");
      setUsers(data.users || []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const createUser = async (e: React.FormEvent) => {
    e.preventDefault();
    // password 字段是新用户密码；operator_password 是当前操作者的二次验证密码，两者不同
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setCreating(true);
    setMsg(null);
    setErr(null);
    try {
      await apiFetch("/api/auth/users", {
        method: "POST",
        body: JSON.stringify({
          username: newName.trim(),
          password: newPass,
          role: newRole,
          operator_password: operatorPassword,
        }),
      });
      setMsg(`用户 ${newName.trim()} 已创建`);
      setNewName("");
      setNewPass("");
      setNewRole("user");
      await load();
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const removeUser = async (u: UserRow) => {
    if (!window.confirm(`确认删除用户 ${u.username}？该操作不可恢复。`)) return;
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return;
    setMsg(null);
    setErr(null);
    try {
      await apiFetch(`/api/auth/users/${u.id}`, {
        method: "DELETE",
        body: JSON.stringify({ operator_password: operatorPassword }),
      });
      setMsg(`用户 ${u.username} 已删除`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "删除失败");
    }
  };

  // 角色变更（用户权限管理）：与创建/删除同款操作密码第二因子；
  // 返回 bool 供 RoleCell 回滚下拉框显示值（取消/失败时）
  const changeRole = async (u: UserRow, role: string): Promise<boolean> => {
    const operatorPassword = await askPassword();
    if (operatorPassword === null) return false;
    setMsg(null);
    setErr(null);
    try {
      const r = await apiFetch<{ message?: string }>(
        `/api/auth/users/${u.id}/role`,
        {
          method: "PUT",
          body: JSON.stringify({ role, operator_password: operatorPassword }),
        },
      );
      setMsg(r.message || `用户 ${u.username} 的角色已更新为 ${ROLE_LABEL[role] ?? role}`);
      await load();
      return true;
    } catch (e) {
      setErr(e instanceof Error ? e.message : "角色更新失败");
      return false;
    }
  };

  return (
    <div className="mx-auto h-full max-w-3xl overflow-y-auto p-8">
      <h1 className="mb-1 text-xl font-semibold tracking-tight">用户管理</h1>
      <p className="text-muted-foreground mb-6 text-sm">
        当前登录：{me?.username ?? "-"}（
        {ROLE_LABEL[me?.role ?? ""] ?? me?.role ?? "-"}）。 仅管理员可访问本页。
      </p>

      {err && (
        <p className="mb-4 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {err}
        </p>
      )}
      {msg && (
        <p className="mb-4 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
          {msg}
        </p>
      )}

      {/* 创建用户 */}
      <form
        onSubmit={createUser}
        className="mb-8 flex flex-wrap items-end gap-3 rounded-lg border p-4"
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="nu-name">用户名</Label>
          <Input
            id="nu-name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            required
            minLength={2}
            className="w-44"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="nu-pass">密码</Label>
          <PasswordInput
            id="nu-pass"
            value={newPass}
            onChange={(e) => setNewPass(e.target.value)}
            required
            minLength={6}
            className="w-44"
            autoComplete="new-password"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="nu-role">角色</Label>
          <select
            id="nu-role"
            value={newRole}
            onChange={(e) => setNewRole(e.target.value)}
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          >
            <option value="user">普通用户</option>
            <option value="readonly">只读用户</option>
            <option value="admin">管理员</option>
          </select>
        </div>
        <Button
          type="submit"
          disabled={creating}
        >
          {creating ? "创建中…" : "创建用户"}
        </Button>
      </form>

      {/* 用户列表 */}
      {loading ? (
        <p className="text-muted-foreground text-sm">加载中…</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-[#6e6e6e]">
              <th className="py-2 pr-4 font-medium">ID</th>
              <th className="py-2 pr-4 font-medium">用户名</th>
              <th className="py-2 pr-4 font-medium">角色</th>
              <th className="py-2 pr-4 font-medium">创建时间</th>
              <th className="py-2 pr-4 font-medium">最后登录</th>
              <th className="py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr
                key={u.id}
                className="border-b last:border-0"
              >
                <td className="py-2 pr-4 text-[#6e6e6e]">{u.id}</td>
                <td className="py-2 pr-4">{u.username}</td>
                <td className="py-2 pr-4">
                  <RoleCell u={u} me={me} onRoleChange={changeRole} />
                </td>
                <td className="py-2 pr-4 text-[#6e6e6e]">{u.created_at}</td>
                <td className="py-2 pr-4 text-[#6e6e6e]">
                  {u.last_login ?? "从未登录"}
                </td>
                <td className="py-2">
                  {u.username !== "admin" && u.id !== me?.user_id && (
                    <button
                      type="button"
                      onClick={() => removeUser(u)}
                      className="text-rose-600 hover:underline"
                    >
                      删除
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* 操作密码确认弹窗（高危操作第二因子） */}
      {operatorPasswordModal}
    </div>
  );
}
