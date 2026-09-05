import { NextRequest, NextResponse } from "next/server";

// Management API（data-engine，默认 http://localhost:2025）的服务端代理。
// 浏览器只请求 /api/mgmt/*，由本路由转发到 Management API。
//
// 认证模型：本代理只透传用户的 Bearer token——
// 不再附加 X-API-Key（那是 system 全权凭据，附加即 confused deputy：
// 未登录/凭据失效的请求都会被后端按 system 放行，全部 admin 闸失效）。
// 无 Bearer → 后端 401 → 前端 auth-guard 跳登录，这是正确行为。
// MGMT_API_KEY 仅用于真实的服务到服务场景（不经本代理）。

// 注意：必须用 127.0.0.1 而非 localhost——Windows 下 Node (undici) 会把
// localhost 解析为 IPv6 ::1，而 Management API 只监听 IPv4，会 ECONNREFUSED
const MGMT_API_URL = process.env.MGMT_API_URL ?? "http://127.0.0.1:2025";

// 本机回环令牌：本地无密码模式下，后端敏感面（审批/权限写/备份/
// 停机）要求 X-Loopback-Token 与 config/runtime/loopback.token 一致——
// 浏览器不可见；由本代理在服务端读取注入（文件不进响应、不进浏览器包）。
import { readFileSync } from "node:fs";
import path from "node:path";
import { isCrossSiteHeaders } from "@/lib/cross-site";

function loopbackToken(): string {
  try {
    const p = path.join(
      process.cwd(),
      "..",
      "data-engine",
      "config",
      "runtime",
      "loopback.token",
    );
    const j = JSON.parse(readFileSync(p, "utf-8"));
    return j.token ?? "";
  } catch {
    return ""; // 令牌未生成（后端未起）——请求会 403，如实呈现
  }
}

type RouteContext = { params: Promise<{ _path: string[] }> };

// 代理层 Origin 防伪：本代理剥离浏览器 Origin 并
// 注入回环令牌——不校验来源的话，恶意网页经本代理即可获得后端全部能力
//（no-cors POST /api/mgmt/stop 即可关停整套后端）。Origin 非本机即拒；
// 无 Origin 放行（curl/本地脚本不携带 Origin——后端令牌闸仍会拦它们，
// 本层只拦浏览器跨站）。
function isCrossSite(request: NextRequest): boolean {
  return isCrossSiteHeaders((name) => request.headers.get(name));
}

async function proxy(request: NextRequest): Promise<Response> {
  if (isCrossSite(request)) {
    return NextResponse.json(
      { detail: "跨站请求已拒绝（代理层防伪）" },
      { status: 403 },
    );
  }
  // 直接取原始 URL 的路径，保留百分号编码（表名等可能含中文/特殊字符）
  const path = request.nextUrl.pathname.replace(/^\/api\/mgmt\/?/, "");
  const url = `${MGMT_API_URL}/${path}${request.nextUrl.search}`;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  // Range 请求透传：浏览器 PDF 查看器按片段拉取大文件的关键
  const range = request.headers.get("range");
  if (range) headers.set("range", range);
  // Bearer token 透传：客户端登录后的用户身份（唯一身份来源）
  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("Authorization", authorization);
  // 本机回环令牌注入（服务端注入，浏览器不可见）——后端敏感面防伪闸
  const lb = loopbackToken();
  if (lb) headers.set("X-Loopback-Token", lb);

  const init: RequestInit = { method: request.method, headers };
  if (request.method !== "GET" && request.method !== "HEAD") {
    // 流式转发请求体（JSON / FormData / 文件上传均适用）
    init.body = request.body;
    (init as RequestInit & { duplex: "half" }).duplex = "half";
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch {
    return NextResponse.json(
      { detail: `Management API 不可达（${MGMT_API_URL}）` },
      { status: 502 },
    );
  }

  // 流式转发响应，保留 Content-Type（JSON / CSV 下载 / SSE 等）与下载文件名
  const respHeaders = new Headers();
  const upstreamContentType = upstream.headers.get("content-type");
  if (upstreamContentType) respHeaders.set("content-type", upstreamContentType);
  const contentDisposition = upstream.headers.get("content-disposition");
  if (contentDisposition)
    respHeaders.set("content-disposition", contentDisposition);
  // Range/流式相关头透传（PDF 分段加载必需）
  for (const h of [
    "accept-ranges",
    "content-range",
    "content-length",
    "etag",
    "cache-control",
    "last-modified",
  ]) {
    const v = upstream.headers.get(h);
    if (v) respHeaders.set(h, v);
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
}

export async function GET(request: NextRequest, _context: RouteContext) {
  return proxy(request);
}

export async function POST(request: NextRequest, _context: RouteContext) {
  return proxy(request);
}

export async function PUT(request: NextRequest, _context: RouteContext) {
  return proxy(request);
}

export async function DELETE(request: NextRequest, _context: RouteContext) {
  return proxy(request);
}

export async function PATCH(request: NextRequest, _context: RouteContext) {
  return proxy(request);
}
