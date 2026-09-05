"use client";
/** 认证模块——token 持久化 + 当前用户 + 登录/登出（迭代 1.5）
 *
 * token 存 localStorage（键 sao:token），用户信息缓存 sao:user。
 * 模块加载时向 apiFetch 注册：Authorization 头提供器 + 401 处理器。
 *
 * 系统模式（后端 API_KEY_ENABLED=false）：/api/auth/me 返回 role=system，
 * AuthGuard 视为"无需登录"，不跳转。
 *
 * 登录状态单一事实来源（20260903 修复"退出后重登按钮消失"）：
 * AppSidebar 挂在根 layout，登录页 router.replace("/") 是 SPA 客户端跳转，
 * 侧边栏不重新挂载——组件本地 state 感知不到 login/logout 的变化。
 * 故由本模块持有用户状态并向订阅者广播（useAuth）：
 * login 成功、logout、fetchMe 校准都经 _setState 收口，组件即时同步。
 * 保留原设计初衷：缓存先行防闪屏、system 模式不显示用户区、
 * 非 401 错误（429 限速等）回退缓存不断登录态。
 */
import { useEffect, useState } from "react";
import {
  apiFetch,
  ApiError,
  MGMT_API,
  setAuthHeaderProvider,
  setUnauthorizedHandler,
} from "@/lib/api-fetch";

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

// ── 登录状态 store（模块级单例 + 订阅广播）──

interface AuthState {
  /** 当前用户；null = 未登录或 system 模式（不显示用户区） */
  me: AuthUser | null;
}

let _state: AuthState = { me: null };
const _listeners = new Set<() => void>();

// 客户端模块加载时以缓存初始化（缓存先行防闪屏；SSR 恒 null）
if (typeof window !== "undefined") {
  _state = { me: getCachedUser() };
}

function _setState(patch: Partial<AuthState>) {
  const next = { ..._state, ...patch };
  if (next.me?.username === _state.me?.username && next.me?.role === _state.me?.role) {
    // 用户未变化不广播（避免无谓渲染）
    _state = next;
    return;
  }
  _state = next;
  _listeners.forEach((l) => l());
}

/** 订阅登录状态变化；返回取消订阅函数 */
export function subscribeAuth(listener: () => void): () => void {
  _listeners.add(listener);
  return () => {
    _listeners.delete(listener);
  };
}

/** 读取当前登录状态快照 */
export function getAuthState(): AuthState {
  return _state;
}

/** 组件订阅登录状态的 hook——login/logout/fetchMe 校准即时同步；
挂载时后台校准一次（与 AuthGuard 的 fetchMe 并发去重共享） */
export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>(_state);
  useEffect(() => {
    setState(_state); // 挂载时对齐（SPA 跳转期间 store 可能已变）
    const unsub = subscribeAuth(() => setState(_state));
    calibrate();
    return unsub;
  }, []);
  return state;
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
    _setState({ me: null });
    window.location.href = "/login";
  });
}

/** 登录：成功返回用户，失败抛 ApiError */
export async function login(
  username: string,
  password: string,
): Promise<AuthUser> {
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
  const u: AuthUser = {
    user_id: r.user.id,
    username: r.user.username,
    role: r.user.role,
  };
  cacheUser(u);
  _setState({ me: u }); // 广播：SPA 跳转后侧边栏等订阅者即时显示用户区
  return u;
}

/** 公开注册（角色固定 user） */
export async function register(
  username: string,
  password: string,
): Promise<void> {
  await apiFetch("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ username, password }),
    skipUnauthorizedHandler: true,
  });
}

/** 拉取当前用户（验证 token 有效性）；401 返回 null；
非 401 错误（429 限速/网络波动）回退本地缓存——限速是暂时性的，
不能当"未登录"处理（曾把页面爆发打成 429 后用户区消失）。
校准结果经 _setState 广播给订阅者。 */
export async function fetchMe(): Promise<AuthUser | null> {
  let me: AuthUser | null;
  try {
    const u = await apiFetch<AuthUser>("/api/auth/me", {
      skipUnauthorizedHandler: true,
    });
    // 系统模式（role=system）不写缓存——不是真实登录用户
    if (u.role !== "system") cacheUser(u);
    me = u;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      me = null;
    } else {
      me = getCachedUser(); // 非 401：回退缓存（调用方据此判登录态）
    }
  }
  _setState({ me });
  return me;
}

// fetchMe 并发去重：同帧多组件（AuthGuard + useAuth 挂载）共享一次请求
let _fetchMeInflight: Promise<AuthUser | null> | null = null;

/** 幂等校准：无并发请求时才发起新的 /api/auth/me（挂载期专用） */
function calibrate(): Promise<AuthUser | null> {
  if (!_fetchMeInflight) {
    _fetchMeInflight = fetchMe().finally(() => {
      _fetchMeInflight = null;
    });
  }
  return _fetchMeInflight;
}

export function logout() {
  const token = getToken();
  clearAuth();
  _setState({ me: null }); // 广播：用户区即时消失
  // 服务端吊销（tv+1 全端失效，token 版本戳语义）：best-effort 直发——
  // apiFetch 的头提供器此刻已读不到 token（本地已清），故用裸 fetch
  // 带原 token；keepalive 让跳转不中断请求；失败不阻断本地登出
  // （token 本身 24h 过期兜底）
  if (token) {
    fetch(`${MGMT_API}/api/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      keepalive: true,
    }).catch(() => {});
  }
  window.location.href = "/login";
}
