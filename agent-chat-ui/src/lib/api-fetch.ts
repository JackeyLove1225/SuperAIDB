/** apiFetch——Management API 统一请求封装（P2-8 前端工程化）
 *
 * 收敛点：MGMT_API 基址 / JSON 处理 / 错误提取（detail|message）/ 超时中止。
 * 页面不再各自 fetch + resp.json + detail 提取三板斧。
 *
 * 认证（迭代 1.5）：基础层不 import 业务层 auth（防循环依赖），改为回调注册：
 * - setAuthHeaderProvider：auth.ts 注册，返回 Authorization 头值（如 "Bearer xxx"）
 * - setUnauthorizedHandler：auth.ts 注册，401 时调用（清 token + 跳登录）
 */
import { MGMT_API } from "@/lib/mgmt-api";

// 单点定义再导出：消费方只需 import { MGMT_API, apiFetch } from "@/lib/api-fetch"
export { MGMT_API };

export class ApiError extends Error {
  status: number;
  /** 非 2xx 响应的已解析 JSON 体（需读结构化字段时用，如 409 风险报告的 report） */
  data?: unknown;
  constructor(status: number, message: string, data?: unknown) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

type AuthHeaderProvider = () => string | null;
type UnauthorizedHandler = (path: string) => void;

/** 非 2xx 错误消息提取：FastAPI 422 的 detail 是 [{loc, msg, ...}] 数组，
 * 直接 String() 会得到 "[object Object]"——整形成 "字段: 错误消息; ..." 的可读文本 */
function extractErrorMessage(
  data: { detail?: unknown; message?: unknown } | undefined,
  status: number
): string {
  const raw = data && (data.detail || data.message);
  if (Array.isArray(raw)) {
    const parts = raw.map((item) => {
      if (item && typeof item === "object") {
        const { loc, msg } = item as { loc?: unknown; msg?: unknown };
        // loc 形如 ["body", "rows", 0, "name"]：去掉 FastAPI 位置前缀，字段路径用 . 连接
        const segs = Array.isArray(loc) ? loc.map(String) : [];
        if (["body", "query", "path"].includes(segs[0])) segs.shift();
        const text = typeof msg === "string" ? msg : JSON.stringify(item);
        return segs.length > 0 ? `${segs.join(".")}: ${text}` : text;
      }
      return String(item);
    });
    if (parts.length > 0) return parts.join("; ");
  }
  return raw ? String(raw) : `请求失败 (HTTP ${status})`;
}

let _authHeaderProvider: AuthHeaderProvider | null = null;
let _unauthorizedHandler: UnauthorizedHandler | null = null;

export function setAuthHeaderProvider(fn: AuthHeaderProvider) {
  _authHeaderProvider = fn;
}

export function setUnauthorizedHandler(fn: UnauthorizedHandler) {
  _unauthorizedHandler = fn;
}

interface ApiFetchOptions extends RequestInit {
  /** 超时毫秒数（默认 30s，0=不限制；重试时按每次尝试独立计时） */
  timeoutMs?: number;
  /** 跳过 401 处理器（登录/注册等本身会 401 的端点用，避免循环跳转） */
  skipUnauthorizedHandler?: boolean;
  /** 外部取消信号（组件卸载/新请求取代旧请求）：中止后不自动重试，抛 AbortError */
  externalSignal?: AbortSignal;
  /** 失败重试次数（不含首次，默认 0）：仅 5xx/网络错误/超时重试，4xx 立即失败 */
  retries?: number;
  /** 重试退避基准毫秒数（默认 500，实际等待 = base * 2^attempt） */
  backoffBaseMs?: number;
}

/** 发起 Management API 请求并解析 JSON 响应
 *
 * - URL 拼装：path 为相对路径（"/api/x"）时拼 MGMT_API 基址；全址输入
 *   （http(s):// 绝对地址或已带 MGMT_API 前缀）原样直发，不双拼
 * - 非 2xx：抛 ApiError（消息取响应体的 detail 或 message，兜底 HTTP 状态码；
 *   完整响应体挂在 ApiError.data 上，409 风险报告等结构化错误可读）
 * - 响应体非 JSON（网关 HTML 错误页等）：抛 ApiError 且不进入重试——
 *   !ok 时消息含响应文本前 200 字摘要，ok 时报"响应不是合法 JSON"
 * - 超时：抛 ApiError(0, "请求超时（Xs）")，与外部取消的 AbortError 可区分
 * - 401：先触发已注册的 UnauthorizedHandler（skipUnauthorizedHandler 除外）
 * - 空响应体（204/停止类端点）：返回 undefined as T
 * - retries > 0：5xx/网络错误/超时按指数退避自动重试；externalSignal 中止（AbortError）不重试
 */
export async function apiFetch<T = unknown>(
  path: string,
  options: ApiFetchOptions = {},
): Promise<T> {
  const {
    timeoutMs = 30000,
    headers,
    skipUnauthorizedHandler,
    externalSignal,
    retries = 0,
    backoffBaseMs = 500,
    ...init
  } = options;

  let lastError: Error | null = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    // 外部 signal 已取消 → 立即抛出，不再尝试
    if (externalSignal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    const controller = new AbortController();
    // 超时打标记：catch 里据此与外部取消（同为 AbortError）区分
    let timedOut = false;
    const timer =
      timeoutMs > 0
        ? setTimeout(() => {
            timedOut = true;
            controller.abort();
          }, timeoutMs)
        : null;
    // 外部取消联动内部 controller（任一触发都中止本次尝试）
    const onExternalAbort = () => controller.abort();
    externalSignal?.addEventListener("abort", onExternalAbort);
    try {
      const authHeader = _authHeaderProvider?.();
      // URL 拼装：全址输入原样直发，否则拼 MGMT_API 基址——
      // 防双前缀（dashboard 404 事故：调用方传 `${MGMT_API}/api/x` 被拼成 /api/mgmt/api/mgmt/...）。
      // 全址 = http(s):// 绝对地址，或已带 MGMT_API 前缀的路径。
      const url =
        path.startsWith("http") || path === MGMT_API || path.startsWith(`${MGMT_API}/`)
          ? path
          : `${MGMT_API}${path}`;
      const resp = await fetch(url, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          ...(authHeader ? { Authorization: authHeader } : {}),
          ...headers,
        },
        signal: controller.signal,
      });
      const text = await resp.text();
      // 错误分支要读 detail/message，成功分支整体 as T 返回——保持宽松类型
      let data: { detail?: unknown; message?: unknown } | undefined;
      try {
        data = text ? JSON.parse(text) : undefined;
      } catch {
        // 非 JSON 响应体（网关 HTML 错误页等）：抛 ApiError 不重试——
        // 否则 SyntaxError 会落入网络错误分支，导致 4xx 被错误重试
        const summary = text.length > 200 ? `${text.slice(0, 200)}…` : text;
        throw new ApiError(
          resp.status,
          resp.ok ? "响应不是合法 JSON" : `请求失败 (HTTP ${resp.status})：${summary}`
        );
      }
      if (!resp.ok) {
        const msg = extractErrorMessage(data, resp.status);
        // 5xx 且仍有重试额度 → 记录错误后进入退避；其余非 2xx 直接抛
        if (resp.status >= 500 && resp.status < 600 && attempt < retries) {
          lastError = new ApiError(resp.status, msg, data);
        } else {
          if (resp.status === 401 && !skipUnauthorizedHandler) {
            _unauthorizedHandler?.(path);
          }
          throw new ApiError(resp.status, msg, data);
        }
      } else {
        return data as T;
      }
    } catch (e) {
      // 业务错误（4xx、重试耗尽的 5xx、401、非 JSON 响应）不重试，直接抛
      if (e instanceof ApiError) throw e;
      const err = e instanceof Error ? e : new Error(String(e));
      // 外部 signal 取消（含退避期间）→ 不重试，保持 AbortError 语义不变（调用方依赖它静默）
      if (externalSignal?.aborted) throw err;
      // 超时与外部取消可分辨：超时归为"网络错误"类，仍可参与重试
      lastError = timedOut ? new ApiError(0, `请求超时（${timeoutMs / 1000}s）`) : err;
    } finally {
      if (timer) clearTimeout(timer);
      externalSignal?.removeEventListener("abort", onExternalAbort);
    }

    // 指数退避（最后一次不再等待；退避期间外部取消立即抛 AbortError）
    if (attempt < retries) {
      const delay = backoffBaseMs * Math.pow(2, attempt);
      await new Promise<void>((resolve, reject) => {
        const t = setTimeout(resolve, delay);
        if (externalSignal) {
          const onAbort = () => {
            clearTimeout(t);
            reject(new DOMException("Aborted", "AbortError"));
          };
          if (externalSignal.aborted) {
            onAbort();
          } else {
            externalSignal.addEventListener("abort", onAbort, { once: true });
          }
        }
      });
    }
  }

  throw lastError ?? new Error("apiFetch: 未知错误");
}
