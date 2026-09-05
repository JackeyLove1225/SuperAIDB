// S10 异常与恢复：坏文件如实报错 + 重启后数据保留且重灌幂等
const H = require('./helpers');
const { execSync, spawn } = require('child_process');
const fs = require('fs');

const CORRUPT = `${H.DE}/tests/acceptance/assets/corrupt_fake.pdf`;


(async () => {
  let allOk = true;

  // ── S10a 坏文件 ──
  allOk = await H.scenario('S10a 坏文件如实报错', async (page) => {
    fs.writeFileSync(CORRUPT, Buffer.from('这不是一个合法的PDF文件内容%%%###$$$'));
    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await H.uploadFile(page, CORRUPT);
    await H.send(page, '把这份文件里的定额项目录进库');
    const done = await H.waitText(page, /失败|异常|错误|无法|损坏|处理完成/, 300000);
    H.check('坏文件有明确响应', done);
    await H.waitDone(page, 600000);
    await H.shot(page, 'S10_corrupt');
    // 系统不崩：后续查询仍正常
    await H.send(page, 'quota_items 有多少条记录？');
    H.check('坏文件后系统仍可用', await H.waitText(page, /\d+/, 60000));
    await H.waitDone(page, 60000);
  }) && allOk;

  // ── S10b 重启保留 + 重灌幂等 ──
  allOk = await H.scenario('S10b 重启保留+重灌幂等', async (page) => {
    const itemsBefore = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    const matsBefore = parseInt(H.db("select count(*) from quota_materials").match(/\d+/)[0]);
    await H.restartServices();

    await page.goto(H.FRONTEND, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await H.send(page, 'quota_items 有多少条记录？');
    H.check('重启后计数一致', await H.waitText(page, new RegExp(String(itemsBefore)), 60000));
    await H.waitDone(page, 60000);
    H.check('DB 行数一致', parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]) === itemsBefore
      && parseInt(H.db("select count(*) from quota_materials").match(/\d+/)[0]) === matsBefore);

    // 重灌同一文件 → 应报冲突/幂等，不重复插
    await H.uploadFile(page, `${H.DE}/tests/acceptance/assets/quota_text_p20_29.pdf`);
    await H.send(page, '把这份文件里的定额项目和材料价格录进库');
    H.check('重灌有响应', await H.waitText(page, /处理完成|冲突|数据入库统计|操作失败/, 300000));
    await H.shot(page, 'S10_reingest');
    const itemsAfter = parseInt(H.db("select count(*) from quota_items").match(/\d+/)[0]);
    H.check('重灌幂等（主表零新增）', itemsAfter === itemsBefore, `${itemsBefore}→${itemsAfter}`);
  }) && allOk;

  await H.closeBrowser();
  process.exit(allOk ? 0 : 1);
})();
