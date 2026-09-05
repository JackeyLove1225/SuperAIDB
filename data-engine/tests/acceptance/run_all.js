// 验收总控：清空演示表（干净演示态）→ 依次跑 S1-S11 → 汇总
// 用法: node data-engine/tests/acceptance/run_all.js [--skip-ingest]
const { execSync, spawn } = require('child_process');
const fs = require('fs');

const DE = require('path').resolve(__dirname, '..', '..');
const SUITES = [
  ['S1 启动首屏', 's01_startup.js'],
  ['S2 交互式建行业', 's02_industry.js'],
  ['S3+S4 文本/扫描/图片入库', 's03_s04_ingest.js'],
  ['S5 数据核验 + S6 查询五连问', 's05_s06_designer_queries.js'],
  ['S7 记录DML + S8 表结构编辑', 's07_s08_record_schema.js'],
  ['S9 权限演示', 's09_permissions.js'],
  ['S10 异常恢复', 's10_recovery.js'],
  ['S11 清理重演', 's11_rerun.js'],
];

function db(sql) {
  return execSync(
    `python "${DE}/tests/acceptance/dbq.py" "${sql.replace(/"/g, "'")}"`,
    { encoding: 'utf-8' }).trim();
}

async function main() {
  const skipIngest = process.argv.includes('--skip-ingest');
  console.log('########## 验收测试全量（S1-S11）##########');
  console.log(`时间: ${new Date().toLocaleString()}`);

  // 前置：权限残留清零（上次事故的 read_only 残留绝不能再坑一轮）+ 备份 + 清表
  const permYml = `${DE}/config/permissions.yml`;
  if (fs.existsSync(permYml)) {
    console.log('[前置] 发现 permissions.yml 残留，删除并需重启生效');
    fs.unlinkSync(permYml);
    try { execSync(`cd "${DE}" && python agent/management/launcher.py stop`, { stdio: 'pipe' }); } catch {}
    const c = spawn('python', ['agent/management/launcher.py'], { cwd: DE, detached: true, stdio: 'ignore' });
    c.unref();
    await new Promise(r => setTimeout(r, 45000));
  }
  if (!skipIngest) {
    console.log('\n[前置] 备份数据库…');
    execSync(`python -c "import shutil,time; shutil.copy('${DE}/db/data_engine.db','${DE}/db/backups/data_engine_acceptance_%s.db')" `
      .replace('%s', new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '').slice(0, 14)));
    console.log('[前置] 清空 quota 四表（干净演示态）…');
    for (const t of ['quota_materials', 'quota_labor', 'quota_machines', 'quota_items']) {
      db(`delete from ${t}`);
    }
    console.log('[前置] quota_items 行数:', db('select count(*) from quota_items'));
  }

  const results = [];
  for (const [name, file] of SUITES) {
    if (skipIngest && file === 's03_s04_ingest.js') {
      console.log(`\n===== ${name}: 跳过（--skip-ingest）=====`);
      results.push([name, 'SKIP']);
      continue;
    }
    console.log(`\n\n===== 开始 ${name} =====`);
    const t0 = Date.now();
    const r = spawn('node', [`${DE}/tests/acceptance/${file}`], { stdio: 'inherit' });
    const code = await new Promise(res => r.on('close', res));
    const mins = ((Date.now() - t0) / 60000).toFixed(1);
    results.push([name, code === 0 ? 'PASS' : 'FAIL', `${mins}min`]);
    console.log(`===== ${name}: ${code === 0 ? 'PASS' : 'FAIL'} (${mins}min) =====`);
  }

  console.log('\n\n########## 验收汇总 ##########');
  let fails = 0;
  for (const [name, status, dur] of results) {
    if (status === 'FAIL') fails++;
    console.log(`  ${status === 'PASS' ? '✅' : status === 'SKIP' ? '⏭️' : '❌'} ${name} ${dur || ''}`);
  }
  console.log(`\n总计: ${results.filter(r => r[1] === 'PASS').length} 过 / ${fails} 败 / ${results.filter(r => r[1] === 'SKIP').length} 跳`);
  console.log(`截图目录: ${require('path').resolve(DE, '..', 'demo_shots', 'acceptance')}/`);
  process.exit(fails === 0 ? 0 : 1);
}

main();
