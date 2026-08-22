// S1 启动与首屏（框架冒烟）
const H = require('./helpers');

(async () => {
  const ok = await H.scenario('S1 启动与首屏', async (page) => {
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(3000);
    H.check('首屏可打开', await page.locator('textarea').count() > 0);
    H.check('推荐问句可见', await H.waitText(page, /有什么可以帮你|把文件变成/, 15000));
    H.check('无 Next.js 错误页',
      await page.locator('text=/Application error|Internal Server Error/i').count() === 0);
    await H.shot(page, 'S1_home');
  });
  await H.closeBrowser();
  process.exit(ok ? 0 : 1);
})();
