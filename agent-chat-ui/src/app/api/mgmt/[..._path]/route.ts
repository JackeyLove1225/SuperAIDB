import { NextRequest, NextResponse } from "next/server";

// Management API（data-engine，默认 http://localhost:2025）的服务端代理。
// 浏览器只请求 /api/mgmt/*，由本路由转发到 Management API。
//
// 认证模型（评审四轮 S-3 根修）：本代理只透传用户的 Bearer token——
// 不再附加 X-API-Key（那是 system 全权凭据，附加即 confused deputy：
// 未登录/凭据失效的请求都会被后端按 system 放行，全部 admin 闸失效）。
// 无 Bearer → 后端 401 → 前端 auth-guard 跳登录，这是正确行为。
// MGMT_API_KEY 仅用于真实的服务到服务场景（不经本代理）。

// 注意：必须用 127.0.0.1 而非 localhost——Windows 下 Node (undici) 会把
// localhost 解析为 IPv6 ::1，而 Management API 只监听 IPv4，会 ECONNREFUSED
const MGMT_API_URL = process.env.MGMT_API_URL ?? "http://127.0.0.1:2025";

type RouteContext = { params: Promise<{ _path: string[] }> };

async function proxy(request: NextRequest): Promise<Response> {
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
  if (upstreamContentType)
    respHeaders.set("content-type", upstreamContentType);
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
