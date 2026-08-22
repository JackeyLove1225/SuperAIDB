// S3 文本 PDF 入库 + S4 扫描件/图片入库（长耗时：OCR + AI 提取）
// 前置：run_all 已清空 quota 四表（干净演示态）
const H = require('./helpers');

const TEXT_PDF = `${H.DE}/tests/acceptance/assets/quota_text_p20_29.pdf`;
const SCAN_PDF = `${H.DE}/tests/acceptance/assets/quota_scan_p34_39.pdf`;
const IMG = `${H.DE}/tests/acceptance/assets/quota_p21.png`;

(async () => {
  let allOk = true;

  // ── S3 文本 PDF 入库 ──
  allOk = await H.scenario('S3 文本PDF入库', async (page) => {
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await H.uploadFile(page, TEXT_PDF);
    await H.shot(page, 'S3_uploaded');
    await H.send(page, '把这份文件里的定额项目和材料价格录进库');
    const done = await H.waitText(page, /处理完成|数据入库统计|操作失败|处理异常/, 300000);
    H.check('入库完成（界面出报告）', done);
    await H.shot(page, 'S3_report');
    H.check('报告无失败字样', await page.locator('text=/操作失败|处理异常|系统性错误/').count() === 0);
    // 底层：主表+材料都有数据，且真名真价
    const items = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    const mats = parseInt(H.db("select count(*) from quota_materials").match(/\d+/)[0]);
    H.check('主表行数 >10', items > 10, items);
    H.check('材料行数 >30', mats > 30, mats);
    const named = parseInt(H.db("select count(*) from quota_items where quota_name is not null").match(/\d+/)[0]);
    H.check('主表名称非空占比 >70%', named / items > 0.7, `${named}/${items}`);
    const fk = parseInt(H.db("select count(*) from quota_materials where quota_item_id is not null").match(/\d+/)[0]);
    H.check('材料外键全部回填', fk === mats, `${fk}/${mats}`);
  }) && allOk;

  // ── S4 扫描件 + 图片入库 ──
  allOk = await H.scenario('S4 扫描件与图片入库', async (page) => {
    const beforeItems = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    const beforeMats = parseInt(H.db("select count(*) from quota_materials").match(/\d+/)[0]);

    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await H.uploadFile(page, SCAN_PDF);
    await H.send(page, '把这份扫描件里的定额项目和材料价格录进库');
    const done = await H.waitText(page, /处理完成|数据入库统计|操作失败|处理异常/, 300000);
    H.check('扫描件入库完成', done);
    await H.shot(page, 'S4_scan_report');
    H.check('扫描件报告无失败字样', await page.locator('text=/操作失败|处理异常|系统性错误/').count() === 0);
    const afterItems = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    const afterMats = parseInt(H.db("select count(*) from quota_materials").match(/\d+/)[0]);
    H.check('扫描件新增主表 ≥8 条', afterItems - beforeItems >= 8, `+${afterItems - beforeItems}`);
    H.check('扫描件新增材料 ≥20 条', afterMats - beforeMats >= 20, `+${afterMats - beforeMats}`);

    // 图片（与已入库区间重叠 → 幂等）
    await H.uploadFile(page, IMG);
    await H.send(page, '把这张图片里的定额项目录进库');
    const imgDone = await H.waitText(page, /处理完成|数据入库统计|冲突|操作失败|处理异常/, 300000);
    H.check('图片入库有响应', imgDone);
    await H.shot(page, 'S4_image_report');
    const finalItems = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    H.check('图片幂等（主表无异常新增）', finalItems - afterItems <= 2, `+${finalItems - afterItems}`);
  }) && allOk;

  await H.closeBrowser();
  process.exit(allOk ? 0 : 1);
})();
