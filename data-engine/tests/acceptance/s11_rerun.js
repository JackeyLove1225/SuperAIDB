// S11 清理与重演：验收残留清零 + 关键查询可重复（可重复演示 = 可重复测试）
const H = require('./helpers');
const fs = require('fs');

(async () => {
  const ok = await H.scenario('S11 清理与重演', async (page) => {
    // 1. 验收残留清零（测试码/权限行/测试列应已不存在）
    const leftovers = H.db("select count(*) from quota_items where quota_code like 'TEST-%' or quota_code like 'PERM-%'");
    H.check('无测试码残留', leftovers.includes('0'), leftovers);
    H.check('无测试列残留',
      H.db("select count(*) from pragma_table_info('quota_items') where name='test_note'").includes('0'));
    H.check('权限文件已清除', !fs.existsSync(`${H.DE}/config/permissions.yml`));
    // 演示数据完好
    const items = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    const mats = parseInt(H.db("select count(*) from quota_materials").match(/\d+/)[0]);
    H.check('演示数据完好（主表>10 材料>30）', items > 10 && mats > 30, `${items}/${mats}`);

    // 2. 重演关键查询（与 S6 同口径）
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await H.send(page, '现在数据库里有哪些表？');
    H.check('重演 Q1', await H.waitText(page, /quota_items|quota_materials/, 60000));
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await H.send(page, 'quota_items 有多少条记录？');
    H.check('重演 Q2 计数一致', await H.waitText(page, new RegExp(String(items)), 60000));
    await H.shot(page, 'S11_rerun');
  });
  await H.closeBrowser();
  process.exit(ok ? 0 : 1);
})();
