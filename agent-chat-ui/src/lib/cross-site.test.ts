// 代理层 Origin 防伪（cross-site.ts）单测（node:test + tsx，零新增依赖）
//
// 锁定契约：sec-fetch-site: cross-site 即拒 / 非本机 Origin 拒 / 本机 Origin 放行 /
// 无 Origin 放行（后端令牌闸兜底）/ Origin 解析失败 fail-closed 拒。
// 该闸是代理通道浏览器跨站的唯一屏障——代理转发剥离来源头并注入回环令牌，
// 判定错误即静默放开写面，回归代价最高。
//
// 运行：npx tsx src/lib/cross-site.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { isCrossSiteHeaders } from "./cross-site";

const h = (headers: Record<string, string>) => (name: string) =>
  headers[name.toLowerCase()] ?? null;

test("sec-fetch-site: cross-site 即拒", () => {
  assert.equal(isCrossSiteHeaders(h({ "sec-fetch-site": "cross-site" })), true);
});

test("sec-fetch-site 大小写变体同拒", () => {
  assert.equal(isCrossSiteHeaders(h({ "sec-fetch-site": "Cross-Site" })), true);
});

test("非本机 Origin 拒", () => {
  assert.equal(isCrossSiteHeaders(h({ origin: "https://evil.example" })), true);
});

test("本机 Origin 放行（localhost / 127.0.0.1 / ::1）", () => {
  assert.equal(
    isCrossSiteHeaders(h({ origin: "http://localhost:3000" })),
    false,
  );
  assert.equal(
    isCrossSiteHeaders(h({ origin: "http://127.0.0.1:3000" })),
    false,
  );
  assert.equal(isCrossSiteHeaders(h({ origin: "http://[::1]:3000" })), false);
});

test("无 Origin 放行（curl/本地脚本由后端令牌闸兜底）", () => {
  assert.equal(isCrossSiteHeaders(h({})), false);
});

test("Origin 解析失败 fail-closed 拒", () => {
  assert.equal(isCrossSiteHeaders(h({ origin: "://not-a-url" })), true);
});
