// 代理层 Origin 防伪的纯逻辑（从 app/api/mgmt/[..._path]/route.ts 抽出，
// 便于 node:test 直测）：本代理剥离浏览器 Origin 并注入回环令牌——
// 不校验来源的话，恶意网页经本代理即可获得后端全部能力
//（no-cors POST /api/mgmt/stop 即可关停整套后端）。
// Origin 非本机即拒；无 Origin 放行（curl/本地脚本不携带 Origin——
// 后端令牌闸仍会拦它们，本层只拦浏览器跨站）。

const LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "::1"];

export function isCrossSiteHeaders(
  get: (name: string) => string | null,
): boolean {
  const sfs = (get("sec-fetch-site") || "").toLowerCase();
  if (sfs === "cross-site") return true;
  const origin = get("origin") || "";
  if (!origin) return false;
  try {
    // WHATWG URL 对 IPv6 字面量返回带方括号形式（[::1]）——归一化去括号再比对
    const host = new URL(origin).hostname.toLowerCase().replace(/^\[|\]$/g, "");
    return !LOOPBACK_HOSTS.includes(host);
  } catch {
    return true; // Origin 解析失败按跨站拒（fail-closed）
  }
}
