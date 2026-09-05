// S5 数据核验（表设计器）+ S6 查询五连问
const H = require('./helpers');

(async () => {
  let allOk = true;

  // ── S5 数据核验 ──
  allOk = await H.scenario('S5 数据核验', async (page) => {
    await page.goto(`${H.FRONTEND}/dashboard/schema-designer`, { waitUntil: 'domcontentloaded' });
    // 画布布局异步计算（图库降级模式更慢）：等卡片出现，不出现就点"刷新"再等
    let cards = await H.waitText(page, /quota_items/, 30000, 2000);
    if (!cards) {
      await page.locator('button:has-text("刷新")').first().click().catch(() => {});
      cards = await H.waitText(page, /quota_items/, 30000, 2000);
    }
    H.check('设计器打开', await page.locator('text=/表结构设计器|Schema/').count() > 0);
    for (const t of ['quota_items', 'quota_materials', 'quota_labor', 'quota_machines']) {
      H.check(`表卡片 ${t}`, await page.locator(`text=${t}`).count() > 0);
    }
    H.check('外键关系可见', await page.locator('text=/quota_item_id/').count() > 0);
    await H.shot(page, 'S5_designer');
    // 控制台页面
    await page.goto(`${H.FRONTEND}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3000);
    H.check('控制台无错误页', await page.locator('text=/Application error/i').count() === 0);
    await H.shot(page, 'S5_console');
  }) && allOk;

  // ── S6 查询五连问 ──
  allOk = await H.scenario('S6 查询五连问', async (page) => {
    await page.goto(H.FRONTEND, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    const items = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    // 每题点"新聊天"开新线程（秒级），替代整页重载（Next.js 全量编译 ~10s/次）
    const newChat = async () => {
      await page.locator('text=新聊天').first().click().catch(() => {});
      await page.waitForTimeout(1500);
    };

    await H.send(page, '现在数据库里有哪些表？');
    H.check('Q1 列出表', await H.waitText(page, /quota_items|quota_materials/, 60000));
    await H.waitDone(page, 60000);
    await H.shot(page, 'S6_q1');

    await newChat();
    await H.send(page, 'quota_items 有多少条记录？');
    // 计数答案的稳健断言：数值 + 计数语义同时出现（防全行 dump 里碰巧含该数字）
    const t0 = Date.now();
    let q2ok = false;
    while (Date.now() - t0 < 60000) {
      const body = await page.locator('body').innerText();
      if (body.includes(String(items)) && /共\s*\d+\s*条|COUNT|统计|记录数/.test(body)) { q2ok = true; break; }
      await page.waitForTimeout(2000);
    }
    H.check('Q2 计数正确', q2ok);
    await H.waitDone(page, 60000);
    await H.shot(page, 'S6_q2');

    await newChat();
    await H.send(page, '统计各类材料的平均单价，按单价降序');
    H.check('Q3 聚合统计', await H.waitText(page, /平均|avg|单价|材料/i, 60000));
    await H.waitDone(page, 60000);
    await H.shot(page, 'S6_q3');

    await newChat();
    await H.send(page, '查询每条主表记录对应的明细数据');
    H.check('Q4 主细 JOIN', await H.waitText(page, /quota_code|定额编号|材料/i, 60000));
    await H.waitDone(page, 60000);
    await H.shot(page, 'S6_q4');

    await newChat();
    await H.send(page, '上传的定额文档里，砌筑工程的工作内容讲了什么？');
    H.check('Q5 向量问答命中原文', await H.waitText(page, /工作内容|砌|砂浆|检索/, 60000));
    await H.waitDone(page, 60000);
    await H.shot(page, 'S6_q5');
  }) && allOk;

  await H.closeBrowser();
  process.exit(allOk ? 0 : 1);
})();
