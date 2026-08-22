// 验收测试共享助手（S1-S11 用户行为主线）
// 规则：所有操作模拟真实用户（真实浏览器界面），断言落在 用户可见结果 + DB 真实状态
const path = require('path');

const DE = path.resolve(__dirname, '..', '..');   // data-engine/
const WORKSPACE = path.resolve(DE, '..');         // 仓根/工作区（agent-chat-ui 与 data-engine 同级）
const PW = process.env.PLAYWRIGHT_PATH ||
  path.join(WORKSPACE, 'agent-chat-ui', 'node_modules', '@playwright', 'test');
const { chromium } = require(PW);
const { execSync } = require('child_process');
const fs = require('fs');

const FRONTEND = 'http://localhost:3000';
const MGMT = 'http://127.0.0.1:2025';
// 管理端密钥从环境注入（MGMT_API_KEY 或 API_KEY），不落仓库
const MGMT_KEY = process.env.MGMT_API_KEY || process.env.API_KEY || '';
if (!MGMT_KEY) console.warn('[helpers] 未设置 MGMT_API_KEY，涉及管理接口的用例会 401');
const SHOTS = path.join(WORKSPACE, 'demo_shots', 'acceptance');

fs.mkdirSync(SHOTS, { recursive: true });

let _browser = null;
async function browser() {
  if (!_browser) _browser = await chromium.launch({ channel: 'chrome', headless: true });
  return _browser;
}
async function newPage() {
  const b = await browser();
  const page = await b.newPage({ viewport: { width: 1600, height: 950 } });
  page.setDefaultTimeout(30000);
  return page;
}
async function closeBrowser() { if (_browser) { await _browser.close(); _browser = null; } }

// ── 用户动作 ──
// 流式期间：textarea 禁用、按钮换成「取消」；结束态：「发送」(type=submit) 在场且可用。
// LangGraph 一次运行有多个 LLM 阶段，isLoading 会在阶段间短暂回落（假结束），
// 必须要求"结束态"连续稳定出现才认可。
async function _idle(page) {
  const submit = page.locator('button[type="submit"]').first();
  if (await submit.count() === 0) return false;
  return await submit.isEnabled().catch(() => false);
}
async function waitDone(page, timeout = 60000) {
  const t0 = Date.now();
  let stable = 0;
  while (Date.now() - t0 < timeout) {
    if (await _idle(page)) {
      stable += 1;
      if (stable >= 3) {           // 连续 3 次（约 6s）稳定空闲才认
        await page.waitForTimeout(1500);
        return true;
      }
    } else {
      stable = 0;
    }
    await page.waitForTimeout(2000);
  }
  return false;
}
async function send(page, text) {
  const ta = page.locator('textarea').first();
  // 先等聊天 UI 就绪（启动蒙层/首编译期 textarea 未挂载，服务重启后常见）
  await ta.waitFor({ state: 'visible', timeout: 120000 });
  // 再等到稳定空闲（上一轮流式/阶段间假空闲都过滤）
  await waitDone(page, 60000);
  await ta.fill(text);
  await page.locator('button[type="submit"]').first().click();
}
async function ask(page, text, timeout = 60000) {
  await send(page, text);
  return waitDone(page, timeout);
}
async function waitText(page, re, timeout = 60000, poll = 2000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await page.locator(`text=${re}`).count() > 0) return true;
    await page.waitForTimeout(poll);
  }
  return false;
}
async function uploadFile(page, absPath) {
  await page.locator('#chat-upload-input').setInputFiles(absPath);
  await page.waitForTimeout(3000);
}
async function shot(page, name) {
  const p = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  console.log(`  [截图] ${name}.png`);
  return p;
}

// ── 底层断言 ──
function db(sql) {
  // 走 dbq.py（自动提交），SQL 以双括号包裹传入（SQL 内只用单引号）
  return execSync(
    `python "${DE}/tests/acceptance/dbq.py" "${sql.replace(/"/g, "'")}"`,
    { encoding: 'utf-8' }).trim();
}
async function mgmt(path_, opts = {}) {
  const res = await fetch(`${MGMT}${path_}`, {
    method: opts.method || 'GET',
    headers: { 'X-API-Key': MGMT_KEY, 'Content-Type': 'application/json' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  try { return JSON.parse(text); } catch { return text; }
}

// ── 断言与汇总 ──
const results = [];
function check(name, cond, detail = '') {
  results.push({ name, ok: !!cond });
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : ' — ' + String(detail).slice(0, 120)}`);
  if (!cond) { const e = new Error(`断言失败: ${name}`); e.soft = true; throw e; }
}
function summary(suite) {
  const fails = results.filter(r => !r.ok);
  console.log(`\n===== ${suite}: ${results.length - fails.length}/${results.length} 通过 =====`);
  return fails.length === 0;
}

// 场景执行器：单场景失败不中断后续场景（记录失败，汇总见 run_all）
async function scenario(name, fn) {
  console.log(`\n########## ${name}`);
  const page = await newPage();
  try { await fn(page); return true; }
  catch (e) {
    console.log(`  ❌ 场景异常: ${String(e).slice(0, 200)}`);
    try { await shot(page, `FAIL_${name.replace(/\W+/g, '_')}`); } catch {}
    return false;
  } finally { await page.close(); }
}

// ── 服务重启（S2 行业切换后必须重启 LangGraph 进程才生效）──
async function restartServices() {
  const { execSync, spawn } = require('child_process');
  console.log('  [重启] 停止服务…');
  try { execSync(`cd "${DE}" && python agent/management/launcher.py stop`, { stdio: 'pipe' }); } catch {}
  console.log('  [重启] 启动服务…');
  const child = spawn('python', ['agent/management/launcher.py'], {
    cwd: DE, detached: true, stdio: 'ignore',
  });
  child.unref();
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const r2024 = await fetch('http://127.0.0.1:2024/ok');
      const r3000 = await fetch('http://localhost:3000').catch(() => null);
      if (r2024.status === 200 && r3000 && (r3000.status === 200 || r3000.status === 307 || r3000.status === 302)) {
        await new Promise(r => setTimeout(r, 5000));
        console.log('  [重启] 服务就绪（2024+3000）');
        return;
      }
    } catch {}
  }
  throw new Error('服务重启超时');
}

module.exports = { FRONTEND, SHOTS, DE, newPage, closeBrowser, send, ask, waitDone, waitText,
  uploadFile, shot, db, mgmt, check, summary, scenario, restartServices };
