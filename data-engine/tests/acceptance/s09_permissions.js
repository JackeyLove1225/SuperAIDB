// S9 权限演示（ConfigHub 热生效版：写配置→立即拦截→立即恢复，全程零重启）
const H = require('./helpers');
const fs = require('fs');

const PERM_YML = `${H.DE}/config/permissions.yml`;

(async () => {
  const ok = await H.scenario('S9 权限演示', async (page) => {
    try {
      // 1. 配置 primary 只读（写文件即生效，无需重启）
      fs.writeFileSync(PERM_YML,
        'default: full\ndatasources:\n  primary:\n    mode: read_only\n', 'utf-8');

      // 2. 用户尝试写入 → 立即被拦截
      await page.goto(H.FRONTEND, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2000);
      await H.send(page, '往 quota_items 插入一条记录：quota_code=PERM-001，quota_name=权限测试');
      H.check('只读库写入被拦截（界面提示）',
        await H.waitText(page, /权限不足|禁止|read_only|失败/, 90000));
      await H.waitDone(page, 90000);
      await H.shot(page, 'S9_blocked');
      H.check('DB 无 PERM-001（拦截真实生效）',
        H.db("select count(*) from quota_items where quota_code='PERM-001'").includes('0'));

      // 3. 只读库读放行（迁移场景的"读出来"）
      await H.send(page, 'quota_items 有多少条记录？');
      H.check('只读库查询放行', await H.waitText(page, /\d+/, 60000));
      await H.waitDone(page, 60000);

      // 4. 恢复可写（删文件即生效，无需重启）
      if (fs.existsSync(PERM_YML)) fs.unlinkSync(PERM_YML);
      await H.send(page, '往 quota_items 插入一条记录：quota_code=PERM-001，quota_name=权限测试');
      H.check('恢复后写入成功', await H.waitText(page, /已插入|成功/, 90000));
      await H.waitDone(page, 90000);
      H.check('DB 有 PERM-001', H.db("select count(*) from quota_items where quota_code='PERM-001'").includes('1'));
      // 清理
      await H.send(page, '删除 quota_items 中 quota_code 是 PERM-001 的记录');
      await H.waitText(page, /已删除|删除/, 60000);
      await H.waitDone(page, 60000);
      H.check('清理 PERM-001', H.db("select count(*) from quota_items where quota_code='PERM-001'").includes('0'));
    } finally {
      // 无论成败必须恢复权限（残留 read_only 会干掉后续全部写入场景）
      if (fs.existsSync(PERM_YML)) {
        fs.unlinkSync(PERM_YML);
        console.log('  [finally] 权限文件已恢复（删除 read_only 残留）');
      }
    }
  });
  await H.closeBrowser();
  process.exit(ok ? 0 : 1);
})();
