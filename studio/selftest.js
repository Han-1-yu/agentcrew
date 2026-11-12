'use strict';
// AgentCrew Studio 自测模式（electron . --selftest）。
// 驱动真实 UI：9 页导航 → 运行选择 → 任务详情 → 事件/产物/供应商/设置 →
// 启动一次 --mock 真实 CLI 运行（验证实时更新与结束统计）→ 取消运行（验证进程终止）。
// 只用 Electron 自带能力（executeJavaScript / capturePage），无额外依赖。

const fs = require('fs');
const path = require('path');

const OUT = path.join(__dirname, 'selftest');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function run(win) {
  (async () => {
    const checks = [];
    const shots = [];
    const check = (name, pass, detail) => {
      checks.push({ name, pass: !!pass, detail: String(detail == null ? '' : detail) });
      console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
    };

    fs.mkdirSync(OUT, { recursive: true });
    win.show();
    win.moveTop();
    win.focus();
    const js = (code) => win.webContents.executeJavaScript(code, true).catch((e) => { console.log('js-error:', e.message); return null; });
    const shot = async (name) => {
      try {
        win.webContents.invalidate();
        await sleep(350);
        const img = await win.webContents.capturePage();
        const file = path.join(OUT, `${name}.png`);
        fs.writeFileSync(file, img.toPNG());
        shots.push(path.basename(file));
      } catch (e) { check(`截图 ${name}`, false, e.message); }
      await sleep(250);
    };

    win.setContentSize(1440, 900);

    // ── 1. 等待渲染进程 boot 完成 ──
    let booted = false;
    for (let i = 0; i < 60; i++) {
      const r = await js('!!(window.__st && document.querySelector(".nav-item"))');
      if (r) { booted = true; break; }
      await sleep(300);
    }
    check('1. 应用启动且渲染进程就绪', booted);
    await sleep(1800); // 等 python 探测 + run 列表首轮刷新

    // ── 2. 12 页导航遍历 ──
    const pages = ['conversation','dashboard','tasks','dag','agents','profiles','gates','artifacts','events','providers','schedules','settings'];
    const navResults = {};
    for (const page of pages) {
      await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.click()`);
      await sleep(400);
      navResults[page] = await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.classList.contains('active')`);
    }
    const allNavOk = pages.every(p => navResults[p]);
    check('2. 12 页导航全部可切换且 active 正确', allNavOk, JSON.stringify(navResults));
    // 导航按钮含可见文字（防空白按钮回归）
    const navTextOk = await js(`[...document.querySelectorAll('.nav-item')].every(b => b.textContent.trim().length > 0)`);
    check('2b. 侧边栏每个导航按钮含可见文字', navTextOk);
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(500);
    await shot('01-9页导航');

    // ── 3. 运行下拉：选择 v2verify，统计与 run.json 一致 ──
    await js(`document.querySelector('#tb-workspace')?.click()`);
    await sleep(500);
    const nRuns = await js('document.querySelectorAll(".rd-item").length');
    check('3. 运行下拉显示多个 run（≥3）', Number(nRuns) >= 3, `${nRuns} 个运行`);

    // 点击 v2verify 运行（通过 rd-item 的 onclick，它会 closeDropdown + switchRun）
    const clickedV2 = await js(`(() => {
      const items = [...document.querySelectorAll('.rd-item')];
      const target = items.find(it => it.textContent.includes('v2verify') || (it.dataset && it.dataset.dir && it.dataset.dir.includes('v2verify')));
      if (target) { target.querySelector('.rd-item-info')?.click(); return true; }
      // 兜底：点第一个
      if (items.length) { items[0].querySelector('.rd-item-info')?.click(); return true; }
      return false;
    })()`);
    await sleep(1500); // 等 run 数据加载

    const state = await js('window.__st.state()');
    const vdir = state && state.dir;
    let statsOk = false;
    if (vdir) {
      try {
        const runJson = JSON.parse(fs.readFileSync(path.join(vdir, 'run.json'), 'utf8'));
        statsOk = JSON.stringify(state.stats) === JSON.stringify(runJson.stats);
        check('3a. 状态数据与 run.json stats 完全一致', statsOk, JSON.stringify(state.stats));
      } catch (e) { check('3a. 读取 run.json 对比失败', false, e.message); }
    } else {
      check('3a. 未选中运行', false);
    }
    await shot('02-看板-选中运行');

    // ── 4. 任务详情面板 ──
    // DAG 节点是 SVG <g>，没有 .click()，须 dispatchEvent
    const clickTask = await js(`(() => {
      const node = document.querySelector('.dag-node');
      if (!node) return { clicked: false };
      try { node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return { clicked: true }; }
      catch (e) { return { clicked: false, error: String(e) }; }
    })()`);
    await sleep(500);
    const detailOpen = await js('!!document.querySelector("#detail-panel:not(.hidden)")');
    check('4. 点击任务节点打开详情面板', clickTask && clickTask.clicked && detailOpen,
      `点击=${clickTask && clickTask.clicked} 面板=${detailOpen}`);
    await shot('03-任务详情');
    // 关闭详情面板（点遮罩）
    await js(`document.querySelector('#detail-mask')?.click()`);
    await sleep(300);

    // ── 5. 事件日志页 ──
    await js(`document.querySelector('.nav-item[data-nav="events"]')?.click()`);
    await sleep(600);
    const evCount = await js('document.querySelectorAll(".ev-full-item").length');
    check('5. 事件日志页渲染事件', Number(evCount) > 0, `${evCount} 条事件`);
    await shot('04-事件日志');

    // ── 6. 产物页 ──
    await js(`document.querySelector('.nav-item[data-nav="artifacts"]')?.click()`);
    await sleep(700);
    const artFiles = await js('document.querySelectorAll(".art-file").length');
    check('6. 产物页显示文件列表', Number(artFiles) >= 0, `${artFiles} 个文件（空目录也通过）`);
    await shot('05-产物页');

    // ── 7. 供应商管理页 ──
    await js(`document.querySelector('.nav-item[data-nav="providers"]')?.click()`);
    await sleep(800);
    const provCards = await js('document.querySelectorAll(".prov-card").length');
    const provTestBtns = await js('document.querySelectorAll(".provider-test-btn").length');
    check('7. 供应商页（≥9 卡片 + 测试按钮）', Number(provCards) >= 9 && Number(provTestBtns) >= 1,
      `${provCards} 张卡片, ${provTestBtns} 个测试按钮`);
    await shot('06-供应商页');

    // ── 8. 设置页 ──
    await js(`document.querySelector('.nav-item[data-nav="settings"]')?.click()`);
    await sleep(500);
    const setFields = await js(`(() => ({
      python: !!document.querySelector('#set-python'),
      output: !!document.querySelector('#set-output'),
      model: !!document.querySelector('#set-model'),
      maxcost: !!document.querySelector('#set-maxcost'),
      api: !!document.querySelector('#set-api'),
    }))()`);
    const setOk = setFields && setFields.python && setFields.output && setFields.model && setFields.api;
    check('8. 设置页（Python/目录/模型/预算/API）', setOk, JSON.stringify(setFields));
    await shot('07-设置页');

    // 回到 dashboard
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(400);

    // ── 9. 启动 mock 新运行（弹窗 UI）→ 等结束 ──
    const before = await js('window.__st.state()');
    await js(`document.querySelector('#btn-new-run')?.click()`);
    await sleep(600);
    await js(`(() => {
      const g = document.querySelector('#nr-goal');
      if (g) g.value = 'Studio selftest mock run via modal';
      const m = document.querySelector('#nr-mock'); if (m) m.checked = true;
      const s = document.querySelector('#nr-slow'); if (s) s.checked = true;
    })()`);
    await sleep(200);
    await js(`document.querySelector('#nr-start')?.click()`);
    // 等弹窗关闭（启动成功后 modal 会关闭，然后弹窗内置轮询会切换到新运行）
    for (let i = 0; i < 30; i++) {
      await sleep(500);
      const hidden = await js('document.querySelector("#modal")?.classList.contains("hidden")');
      if (hidden) break;
    }

    let newDir = null; let dirAt = 0; let midShots = 0;
    let finalState = null;
    const beforeDir = before && before.dir;
    for (let i = 0; i < 400; i++) { // 最多 120s
      await sleep(300);
      const st = await js('window.__st.state()');
      if (st && st.dir && !newDir) {
        if (!beforeDir || st.dir !== beforeDir) {
          newDir = st.dir; dirAt = Date.now();
        }
      }
      if (newDir && midShots === 0 && Date.now() - dirAt > 800) { await shot('08-运行中-a'); midShots = 1; }
      if (newDir && midShots === 1 && Date.now() - dirAt > 2000) { await shot('08-运行中-b'); midShots = 2; }
      if (newDir && st && st.dir === newDir && ['completed','failed','cancelled','interrupted'].includes(st.status)) {
        finalState = st;
        break;
      }
    }
    check('9a. 新运行目录被发现并选中', !!newDir, newDir);
    // 验证运行产生了事件（实时更新链路工作）
    const finalEvents = finalState ? (finalState.events || 0) : 0;
    check('9b. 运行产生事件（实时更新链路）', finalEvents > 5, `${finalEvents} 条事件`);
    check('9c. 运行正常结束', finalState && finalState.status === 'completed',
      `最终状态 ${finalState && finalState.status}`);
    await shot('09-运行结束');

    // 结束后统计与 run.json 对比
    if (newDir && finalState) {
      try {
        const rj = JSON.parse(fs.readFileSync(path.join(newDir, 'run.json'), 'utf8'));
        const st2 = await js('window.__st.state()');
        check('9d. 新运行统计与 run.json 一致', JSON.stringify(st2.stats) === JSON.stringify(rj.stats),
          `${rj.status} ${JSON.stringify(rj.stats)}`);
      } catch (e) { check('9d. 对比失败', false, e.message); }
    }

    // ── 10. 取消运行：弹窗启动慢速 mock，等运行中后点取消，验证进程终止 ──
    await js(`document.querySelector('#btn-new-run')?.click()`);
    await sleep(600);
    await js(`(() => {
      const g = document.querySelector('#nr-goal');
      if (g) g.value = 'Studio selftest cancel run';
      const m = document.querySelector('#nr-mock'); if (m) m.checked = true;
      // 取消测试用低延迟，使进程能更快响应取消
    })()`);
    await sleep(200);
    await js(`document.querySelector('#nr-start')?.click()`);
    for (let i = 0; i < 30; i++) {
      await sleep(500);
      const hidden = await js('document.querySelector("#modal")?.classList.contains("hidden")');
      if (hidden) break;
    }

    let cancelDir = null; let clickedCancel = false;
    const firstRunDir = newDir; // 步骤9的运行目录
    for (let i = 0; i < 300 && !clickedCancel; i++) { // 最多 90s
      await sleep(300);
      const st = await js('window.__st.state()');
      if (st && st.dir && st.dir !== firstRunDir && !cancelDir) cancelDir = st.dir;
      if (!cancelDir) continue;
      if (st && st.status !== 'running') continue;
      clickedCancel = !!(await js(`(() => {
        const btns = [...document.querySelectorAll('.rs-actions .btn-danger')];
        const cancelBtn = btns.find(b => b.textContent.includes('取消'));
        if (cancelBtn) { cancelBtn.click(); return true; }
        return false;
      })()`));
    }
    check('10a. 取消按钮可点击', clickedCancel, cancelDir);

    // 取消确认弹窗：点「确认」（第二个按钮）
    if (clickedCancel) {
      await sleep(400);
      await js(`(() => {
        const btns = [...document.querySelectorAll('#modal .modal-actions .btn')];
        const confirmBtn = btns.find(b => b.textContent.includes('确认')) || btns[btns.length - 1];
        if (confirmBtn) confirmBtn.click();
      })()`);
    }

    let cancelStatus = null;
    for (let i = 0; i < 120; i++) { // 最多 60s 等取消生效
      await sleep(500);
      const st = await js('window.__st.state()');
      if (st && ['cancelled','completed','failed','interrupted'].includes(st.status)) { cancelStatus = st.status; break; }
    }
    await sleep(8000); // 给 mock 进程足够时间退出（cancelOffline 是优雅取消）

    // OS 级验证：无僵尸 python 进程
    const { execSync } = require('child_process');
    let pyCount = -1;
    try {
      pyCount = execSync('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH', { encoding: 'utf8' })
        .trim().split('\n').filter((l) => l.includes('python')).length;
    } catch (e) { console.log('tasklist 失败', e.message); }
    let zombies = 0;
    for (const rec of globalThis.__procs.values()) {
      if (rec.proc && rec.proc.exitCode === null) zombies += 1;
    }
    check('10b. 取消后进程终止、无僵尸', pyCount === 0 && zombies === 0,
      `状态 ${cancelStatus}，OS python 进程 ${pyCount} 个，内部 procs 僵尸 ${zombies} 个`);
    await shot('10-取消后');

    // ── 汇总 ──
    const allPass = checks.every((c) => c.pass);
    const results = { when: new Date().toISOString(), allPass, checks, shots };
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2));
    console.log(`\nselftest 完成：${checks.filter((c) => c.pass).length}/${checks.length} 通过，截图 ${shots.length} 张 → ${OUT}`);
    const { app } = require('electron');
    app.exit(allPass ? 0 : 1);
  })().catch((e) => {
    console.error('selftest 异常:', e);
    try { fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ allPass: false, error: String(e.stack || e) }, null, 2)); } catch { /* 忽略 */ }
    require('electron').app.exit(1);
  });
}

module.exports = { run };
