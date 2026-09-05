// S2 交互式建行业建表（HITL 中断卡片流：Reject 提意见 → 重设计 → Approve 建成）
// 产品的确认机制是 LangGraph interrupt 卡片（Approve/Reject+Reason），不是聊天输入
const H = require('./helpers');
const fs = require('fs');

async function waitInterruptCard(page, timeout = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await page.locator('button:has-text("Approve")').count() > 0) return true;
    await page.waitForTimeout(3000);
  }
  return false;
}

(async () => {
  const ok = await H.scenario('S2 交互式建行业建表', async (page) => {
    try {
      await page.goto(H.FRONTEND, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2000);

      // 1. 发起建行业
      await H.send(page, '我想创建一个 验收零售 行业的数据库，管理商品、供应商和进货记录');
      H.check('中断确认卡片出现', await waitInterruptCard(page));
      await H.shot(page, 'S2_card');

      // 2. 用户提意见（Reject + Reason）：要求加字段
      await page.locator('textarea[placeholder*="feedback"], textarea[placeholder*="agent"]').first()
        .fill('商品表再加一个 保质期 字段');
      await page.locator('button:has-text("Submit rejection")').first().click();
      H.check('重设计后新卡片出现（含保质期）', await waitInterruptCard(page));
      const revised = await page.locator('text=/保质期/').count() > 0;
      H.check('AI 按意见修改提案（卡片含保质期）', revised);
      await H.shot(page, 'S2_revised_card');

      // 3. 用户批准 → 自动建表
      await page.locator('button:has-text("Approve")').first().click();
      H.check('建表成功提示', await H.waitText(page, /建库执行汇报|建表：成功|成功 4 张/, 60000));
      await H.waitDone(page, 60000);
      await H.shot(page, 'S2_confirmed');

      // 4. 底层断言：零售表真实建成（表名由 AI 设计决定，断结构不断名）
      const newTables = H.db("select name from sqlite_master where type='table' and name not in ('sqlite_sequence','quota_items','quota_labor','quota_machines','quota_materials','users')");
      const nTables = (newTables.match(/\('/g) || []).length;
      H.check('建成 ≥3 张零售表', nTables >= 3, newTables.slice(0, 150));
      // 保质期字段存在于某张零售表（表名由 AI 设计决定：commodities/products/goods…）
      const productTable = ['commodities', 'products', 'goods', 'product']
        .find(t => H.db(`select count(*) from sqlite_master where type='table' and name='${t}'`).includes('1'));
      H.check('商品表存在', !!productTable, newTables.slice(0, 120));
      const cols = H.db(`select name from pragma_table_info('${productTable}')`);
      H.check('商品表含保质期字段', /shelf|保质|expiry/i.test(cols), cols.slice(0, 120));
      // 进货相关表含商品/供应商外键（设计名随 AI 变：收集全部候选表字段一起判）
      const allCols = H.db("select group_concat(name) from pragma_table_info('purchase_records')")
        + H.db("select group_concat(name) from pragma_table_info('purchase_items')")
        + H.db("select group_concat(name) from pragma_table_info('purchase_order_items')")
        + H.db("select group_concat(name) from pragma_table_info('purchase_orders')");
      H.check('进货表含商品/供应商外键', /commodity|product|goods/i.test(allCols) && /supplier/i.test(allCols),
        allCols.slice(0, 150));
    } finally {
      // 无论成败都切回 engineering + 清理零售残留 + 重启
      // （行业切换只写 .env，LangGraph 进程必须重启才生效；
      //   零售表不清理会让后续 S8 撞"单行业 DDL 守卫"——那是真实产品纪律）
      try {
        const sw = await H.mgmt('/api/industries/switch', { method: 'POST', body: { industry: 'engineering' } });
        console.log('  [恢复] 行业已切回 engineering:', JSON.stringify(sw).slice(0, 80));
      } catch (e) {
        console.log('  [恢复] 切回行业失败（需人工检查）:', String(e).slice(0, 100));
      }
      try {
        const keep = "('sqlite_sequence','quota_items','quota_labor','quota_machines','quota_materials','users','roles','permissions','role_permissions','sessions')";
        const tbls = H.db(`select name from sqlite_master where type='table' and name not in ${keep}`);
        for (const m of tbls.matchAll(/'([^']+)'/g)) {
          H.db(`drop table if exists "${m[1]}"`);
          console.log('  [清理] 零售残留表已删:', m[1]);
        }
        require('child_process').execSync(`rm -rf "${H.DE}/industries/retail_acceptance"`, { stdio: 'pipe' });
      } catch (e) {
        console.log('  [清理] 残留清理警告:', String(e).slice(0, 100));
      }
      await H.restartServices();
    }
  });
  await H.closeBrowser();
  process.exit(ok ? 0 : 1);
})();
