/** 媒体签名 URL——data-engine "媒体签名" 机制的前端接线（认证开启后专用）
 *
 * 背景：iframe / <img> / 下载锚点都是浏览器原生请求，带不了 Authorization Bearer，
 * 认证开启后命中 401。后端提供 GET /api/media/sign?path=<路径>&kind=upload|export
 * 换取 { sig, ttl:300 }，把 sig 拼进资源 URL（&sig=<token>）即可放行。
 *
 * 签名对象约定（与后端一致）：
 * - kind=upload：签名对象是 uploads 相对路径原值（/api/files/raw、/api/preview/pdf 共用）
 * - kind=export：签名对象是导出文件名（/api/exports/<filename>/download）
 *
 * 无认证模式下 sign 端点照常可用（中间件全放行），调用方无需模式分叉。
 */
import { apiFetch } from "@/lib/api-fetch";

/** 申请媒体签名，返回 sig token（签名失败抛 ApiError，由调用方报错） */
export async function signMedia(
  path: string,
  kind: "upload" | "export"
): Promise<string> {
  const r = await apiFetch<{ sig: string; ttl: number }>(
    `/api/media/sign?path=${encodeURIComponent(path)}&kind=${kind}`
  );
  return r.sig;
}

/** 给资源 URL 追加 sig 参数（sig 含 . 等字符，必须 encodeURIComponent） */
export function withSig(url: string, sig: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}sig=${encodeURIComponent(sig)}`;
}
