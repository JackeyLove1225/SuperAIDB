// S7 记录级增删改查（聊天指令）+ S8 表结构编辑（含主键保护）
const H = require('./helpers');

// 步进问答：发送 → 等关键词 → 等生成完毕
async function step(page, text, re, name, timeout = 60000) {
  await H.send(page, text);
  const hit = await H.waitText(page, re, timeout);
  await H.waitDone(page, timeout);
  H.check(name, hit);
}

(async () => {
  let allOk = true;

  // ── S7 记录级增删改查 ──
  allOk = await H.scenario('S7 记录级增删改查', async (page) => {
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);

    await step(page, '往 quota_items 插入一条记录：quota_code=TEST-901，quota_name=验收测试墙，unit=10m3，base_price=100',
      /已插入|成功|冲突|失败/, '单条插入有响应');
    H.check('DB 有 TEST-901', H.db("select count(*) from quota_items where quota_code='TEST-901'").includes('1'));

    await step(page, '把 TEST-901 的 base_price 改成 200',
      /已更新|已修改|成功|失败/, '修改有响应');
    const price = H.db("select base_price from quota_items where quota_code='TEST-901'");
    H.check('DB 基价已改 200', price.includes('200'), price);

    await step(page, '往 quota_items 批量插入 2 条：TEST-902（测试墙A，基价50）和 TEST-903（测试墙B，基价60）',
      /已插入|成功|冲突|失败/, '批量插入有响应');
    H.check('DB 有 TEST-902/903',
      H.db("select count(*) from quota_items where quota_code in ('TEST-902','TEST-903')").includes('2'));

    await step(page, '把 TEST-901 的 id 改成 99999',
      /不允许|系统主键|阻止|失败/, '改 id 被阻止');

    await step(page, '删除 quota_items 中 quota_code 是 TEST-901 的记录',
      /已删除|删除|失败/, '删除有响应');
    H.check('DB 无 TEST-901', H.db("select count(*) from quota_items where quota_code='TEST-901'").includes('0'));

    // 批量删（选择集路径）：先查询成选择集，再删整批——这正是产品的批量删除设计
    await step(page, '查询 quota_items 中 quota_code 是 TEST-902 或 TEST-903 的记录',
      /TEST-902|TEST-903|条记录/, '批量删前查询成选择集');
    await step(page, '删除查询到的这些记录',
      /已删除|删除|失败/, '选择集批量删除');
    H.check('DB 无 TEST-902/903',
      H.db("select count(*) from quota_items where quota_code in ('TEST-902','TEST-903')").includes('0'));
    await H.shot(page, 'S7_done');
  }) && allOk;

  // ── S8 表结构编辑 ──
  allOk = await H.scenario('S8 表结构编辑', async (page) => {
    // 前置自愈合：清掉历史 test_note（DB+YAML 双侧残渣会触发一致性守卫阻止加字段）
    try {
      H.db("ALTER TABLE quota_items DROP COLUMN test_note");
      console.log('  [前置] 历史 test_note 已清');
    } catch {}
    await page.goto(H.FRONTEND, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    await step(page, '给 quota_items 加一个 test_note TEXT 字段',
      /已加入|已添加|成功|已存在/, '加字段有响应', 90000);
    H.check('DB 有 test_note 列',
      H.db("select count(*) from pragma_table_info('quota_items') where name='test_note'").includes('1'));

    await page.goto(`${H.FRONTEND}/dashboard/schema-designer`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5000);
    await H.shot(page, 'S8_designer_new_col');

    // 回到聊天页再做下一步（设计器页没有聊天输入框——上轮事故就是在这卡 120s）
    await page.goto(H.FRONTEND, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    await step(page, '删除 quota_items 的 test_note 字段',
      /已删除|成功|失败/, '删字段有响应', 90000);
    H.check('DB 无 test_note 列',
      H.db("select count(*) from pragma_table_info('quota_items') where name='test_note'").includes('0'));

    await step(page, '修改 quota_items 的 id 类型为 TEXT',
      /系统主键|不允许|阻止/, '改主键被阻止');
    await H.shot(page, 'S8_done');
  }) && allOk;

  await H.closeBrowser();
  process.exit(allOk ? 0 : 1);
})();
