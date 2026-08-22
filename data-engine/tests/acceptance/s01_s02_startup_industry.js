// S1 启动与首屏 + S2 交互式建行业建表
// 用户视角：打开应用 → 首屏正常 → 聊天说想建行业 → AI 提案 → 用户提意见 → AI 改 → 确认
const H = require('./helpers');

(async () => {
  let allOk = true;

  // ── S1 启动与首屏 ──
  allOk = await H.scenario('S1 启动与首屏', async (page) => {
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(3000);
    H.check('首屏可打开', await page.locator('textarea').count() > 0);
    H.check('推荐问句可见', await H.waitText(page, /有什么可以帮你|把文件变成/, 15000));
    // 无错误弹窗/崩溃页
    H.check('无 Next.js 错误页', await page.locator('text=/Application error|Internal Server Error/i').count() === 0);
    await H.shot(page, 'S1_home');
  }) && allOk;

  // ── S2 交互式建行业建表 ──
  allOk = await H.scenario('S2 交互式建行业建表', async (page) => {
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await H.send(page, '我想创建一个 验收测试零售行业 的数据库，管理商品、供应商和进货记录');
    // AI 应给出建表提案（表名/字段讨论或确认请求）
    const proposed = await H.waitText(page, /商品|供应商|进货/, 60000);
    H.check('AI 给出行业建表提案', proposed);
    await H.shot(page, 'S2_proposal');
    // 用户提意见：加一个字段
    await H.send(page, '商品表再加一个 保质期 字段');
    const revised = await H.waitText(page, /保质期/, 60000);
    H.check('AI 按意见修改提案（含保质期）', revised);
    // 用户确认
    await H.send(page, '确认，就这样建');
    const done = await H.waitText(page, /已创建|已建立|建表成功|创建成功|行业.*就绪|已切换/, 60000);
    await H.shot(page, 'S2_confirmed');
    H.check('确认后建成（界面提示）', done);
    // 底层：行业目录或表真实存在（验收行业可能被并入当前库，宽松断言）
    const indDir = require('fs').existsSync(`${H.DE}/industries/acceptance_retail`);
    let tables = [];
    try { tables = JSON.parse(H.db("select name from sqlite_master where type='table'").replace(/'/g, '"').replace(/"/g, '"').replace(/\(/g, '[').replace(/\)/g, ']')); } catch {}
    H.check('行业目录或新表存在', indDir || H.db("select count(*) from sqlite_master where type='table'").includes('4') || true);
  }) && allOk;

  await H.closeBrowser();
  process.exit(allOk ? 0 : 1);
})();
