// apiFetch 地基单测（node:test + tsx 执行，零新增依赖，全局 fetch 用 mock 替换后恢复）
//
// 锁定契约：URL 拼装形状（防双前缀）、Bearer 注入、401 处理器、非 2xx 错误提取、
// 超时/外部取消语义、5xx 退避重试。这些是全部页面请求的地基行为，回归代价最高。
//
// 运行：npx tsx src/lib/api-fetch.test.ts
import { test, after } from "node:test";
import assert from "node:assert/strict";
import {
  apiFetch,
  ApiError,
  MGMT_API,
  setAuthHeaderProvider,
  setUnauthorizedHandler,
} from "./api-fetch";

// ── 全局 fetch mock：记录调用 + 可编程响应 ──
type FetchHandler = (url: string, init?: RequestInit) => Promise<Response> | Response;

const origFetch = globalThis.fetch;
let calls: { url: string; init?: RequestInit }[] = [];
let handler: FetchHandler = () => jsonResp({});

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  calls.push({ url, init });
  return handler(url, init);
}) as typeof fetch;

after(() => {
  globalThis.fetch = origFetch;
});

function mockFetch(fn: FetchHandler) {
  calls = [];
  handler = fn;
}

const jsonResp = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** 模拟真实 fetch 的中止语义：挂起直到 signal 触发，然后以 AbortError 拒绝 */
const hangUntilAbort: FetchHandler = (_url, init) =>
  new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () =>
      reject(new DOMException("Aborted", "AbortError"))
    );
  });

// ── URL 拼装形状（防双前缀——dashboard 404 事故）──

test("URL 拼装: 相对路径 /api/x 拼上 MGMT_API 前缀", async () => {
  mockFetch(() => jsonResp({ ok: true }));
  await apiFetch("/api/x");
  assert.equal(calls[0].url, `${MGMT_API}/api/x`);
});

test("URL 拼装: 已带 MGMT_API 前缀的全址原样发出（不双拼）", async () => {
  mockFetch(() => jsonResp({ ok: true }));
  await apiFetch(`${MGMT_API}/api/x`);
  assert.equal(calls[0].url, `${MGMT_API}/api/x`);
});

test("URL 拼装: http(s) 绝对地址原样发出", async () => {
  mockFetch(() => jsonResp({ ok: true }));
  await apiFetch("http://example.com/api/x");
  await apiFetch("https://example.com/api/y");
  assert.equal(calls[0].url, "http://example.com/api/x");
  assert.equal(calls[1].url, "https://example.com/api/y");
});

// ── Bearer 注入 ──

test("Bearer: 注册 authHeaderProvider 后请求带 Authorization", async () => {
  setAuthHeaderProvider(() => "Bearer T123");
  mockFetch(() => jsonResp({}));
  await apiFetch("/api/x");
  const headers = new Headers(calls[0].init?.headers);
  assert.equal(headers.get("Authorization"), "Bearer T123");
});

test("Bearer: provider 返回 null（未登录/未注册）不带 Authorization", async () => {
  setAuthHeaderProvider(() => null);
  mockFetch(() => jsonResp({}));
  await apiFetch("/api/x");
  const headers = new Headers(calls[0].init?.headers);
  assert.equal(headers.get("Authorization"), null);
});

// ── 401 处理器 ──

test("401: 触发 unauthorizedHandler 恰好一次（4xx 不参与重试）", async () => {
  let n = 0;
  setUnauthorizedHandler(() => {
    n++;
  });
  mockFetch(() => jsonResp({ detail: "未认证" }, 401));
  await assert.rejects(
    apiFetch("/api/x", { retries: 2, backoffBaseMs: 1 }),
    (e) => e instanceof ApiError && e.status === 401 && e.message === "未认证"
  );
  assert.equal(n, 1);
  assert.equal(calls.length, 1);
});

test("401: skipUnauthorizedHandler 时不触发", async () => {
  let n = 0;
  setUnauthorizedHandler(() => {
    n++;
  });
  mockFetch(() => jsonResp({ detail: "未认证" }, 401));
  await assert.rejects(
    apiFetch("/api/x", { skipUnauthorizedHandler: true }),
    (e) => e instanceof ApiError && e.status === 401
  );
  assert.equal(n, 0);
});

// ── 非 2xx 错误提取 ──

test("非 2xx: detail 字符串原样成为错误消息", async () => {
  mockFetch(() => jsonResp({ detail: "表不存在" }, 404));
  await assert.rejects(
    apiFetch("/api/x"),
    (e) => e instanceof ApiError && e.status === 404 && e.message === "表不存在"
  );
});

test("非 2xx: FastAPI 422 detail 数组整形成可读文本（去 body/query 前缀，字段路径点连）", async () => {
  mockFetch(() =>
    jsonResp(
      {
        detail: [
          { loc: ["body", "rows", 0, "name"], msg: "field required" },
          { loc: ["query", "limit"], msg: "value is not a valid integer" },
        ],
      },
      422
    )
  );
  await assert.rejects(
    apiFetch("/api/x"),
    (e) =>
      e instanceof ApiError &&
      e.status === 422 &&
      e.message === "rows.0.name: field required; limit: value is not a valid integer"
  );
});

test("非 2xx: 非 JSON 错误页（网关 HTML）不重试，抛 ApiError 含响应摘要", async () => {
  mockFetch(() => new Response("<html><body>Bad Gateway</body></html>", { status: 502 }));
  await assert.rejects(
    apiFetch("/api/x", { retries: 2, backoffBaseMs: 1 }),
    (e) =>
      e instanceof ApiError &&
      e.status === 502 &&
      e.message.includes("HTTP 502") &&
      e.message.includes("Bad Gateway")
  );
  assert.equal(calls.length, 1);
});

// ── 超时与外部取消 ──

test("超时: timedOut 抛 ApiError(0, 请求超时...)", async () => {
  mockFetch(hangUntilAbort);
  await assert.rejects(
    apiFetch("/api/x", { timeoutMs: 30 }),
    (e) => e instanceof ApiError && e.status === 0 && e.message.startsWith("请求超时")
  );
  assert.equal(calls.length, 1);
});

test("外部 signal 中止: 抛 AbortError 且不重试", async () => {
  const ac = new AbortController();
  mockFetch((url, init) => {
    queueMicrotask(() => ac.abort()); // 请求在途时外部取消
    return hangUntilAbort(url, init);
  });
  await assert.rejects(
    apiFetch("/api/x", { externalSignal: ac.signal, retries: 2, backoffBaseMs: 1 }),
    (e) => (e as DOMException).name === "AbortError"
  );
  assert.equal(calls.length, 1);
});

// ── 5xx 退避重试 ──

test("5xx 重试: retries=1 时首次 500、第二次成功则返回成功结果", async () => {
  let n = 0;
  mockFetch(() => {
    n++;
    return n === 1
      ? jsonResp({ detail: "服务器错误" }, 500)
      : jsonResp({ ok: true, value: 42 });
  });
  const r = await apiFetch<{ value: number }>("/api/x", { retries: 1, backoffBaseMs: 1 });
  assert.equal(r.value, 42);
  assert.equal(calls.length, 2);
});

test("5xx 重试: 重试额度耗尽后抛最后一次的 ApiError", async () => {
  mockFetch(() => jsonResp({ detail: "持续故障" }, 503));
  await assert.rejects(
    apiFetch("/api/x", { retries: 1, backoffBaseMs: 1 }),
    (e) => e instanceof ApiError && e.status === 503 && e.message === "持续故障"
  );
  assert.equal(calls.length, 2);
});
