/** 认证模块——token 持久化 + 当前用户 + 登录/登出（迭代 1.5）
 *
 * token 存 localStorage（键 sao:token），用户信息缓存 sao:user。
 * 模块加载时向 apiFetch 注册：Authorization 头提供器 + 401 处理器。
 *
 * 系统模式（后端 API_KEY_ENABLED=false）：/api/auth/me 返回 role=system，
 * AuthGuard 视为"无需登录"，不跳转。
 */
import { apiFetch, ApiError, setAuthHeaderProvider, setUnauthorizedHandler } from "@/lib/api-fetch";

const TOKEN_KEY = "sao:token";
const USER_KEY = "sao:user";

export interface AuthUser {
  user_id: number;
  username: string;
  role: string; // admin | user | readonly | system
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string) {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearAuth() {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

export function getCachedUser(): AuthUser | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as AuthUser) : null;
  } catch {
    return null;
  }
}

function cacheUser(u: AuthUser) {
  window.localStorage.setItem(USER_KEY, JSON.stringify(u));
}

// ── 向 apiFetch 注册认证回调（模块加载即生效，客户端环境）──
if (typeof window !== "undefined") {
  setAuthHeaderProvider(() => {
    const t = getToken();
    return t ? `Bearer ${t}` : null;
  });
  setUnauthorizedHandler(() => {
    // 已登录页/无 token 时不重复跳（避免登录失败触发跳转刷新循环）
    if (window.location.pathname === "/login") return;
    clearAuth();
    window.location.href = "/login";
  });
}

/** 登录：成功返回用户，失败抛 ApiError */
export async function login(username: string, password: string): Promise<AuthUser> {
  const r = await apiFetch<{
    ok: boolean;
    token: string;
    user: { id: number; username: string; role: string };
  }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
    skipUnauthorizedHandler: true, // 登录失败 401 是正常业务反馈，不触发全局跳转
  });
  setToken(r.token);
  const u: AuthUser = { user_id: r.user.id, username: r.user.username, role: r.user.role };
  cacheUser(u);
  return u;
}

/** 公开注册（角色固定 user） */
export async function register(username: string, password: string): Promise<void> {
  await apiFetch("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ username, password }),
    skipUnauthorizedHandler: true,
  });
}

/** 拉取当前用户（验证 token 有效性）；401 返回 null */
export async function fetchMe(): Promise<AuthUser | null> {
  try {
    const u = await apiFetch<AuthUser>("/api/auth/me", { skipUnauthorizedHandler: true });
    // 系统模式（role=system）不写缓存——不是真实登录用户
    if (u.role !== "system") cacheUser(u);
    return u;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export function logout() {
  clearAuth();
  window.location.href = "/login";
}
