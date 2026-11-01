'use strict';
// AgentCrew Studio 边缘用例探针（electron . --probe）
// 聚焦自动化 e2e/selftest 未覆盖的边界：快捷键映射、模态焦点、下拉关闭、
// 空/非法输入、连点、缩放极限、筛选隔离、主题持久化等。只用 Electron 自带能力。

const fs = require('fs');
const path = require('path');
const { app } = require('electron');

const OUT = path.join(__dirname, 'probe-out');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function run(win) {
  (async () => {
    const checks = [];
    const check = (name, pass, detail) => {
      checks.push({ name, pass: !!pass, detail: String(detail == null ? '' : detail) });
      console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
    };

    fs.mkdirSync(OUT, { recursive: true });
    win.show(); win.moveTop(); win.focus();
    const js = (code) => win.webContents.executeJavaScript(code, true).catch((e) => ({ __err: e.message }));
    const nav = () => js(`document.querySelector('.nav-item.active')?.dataset.nav`);
    const shot = async (name) => {
      try {
        win.webContents.invalidate();
        await sleep(250);
        const img = await win.webContents.capturePage();
        fs.writeFileSync(path.join(OUT, `${name}.png`), img.toPNG());
      } catch (e) { /* 截图失败不致命 */ }
    };

    win.setContentSize(1440, 900);

    // 等待启动
    let booted = false;
    for (let i = 0; i < 60; i++) {
      const r = await js('!!(window.__st && document.querySelector(".nav-item"))');
      if (r) { booted = true; break; }
      await sleep(300);
    }
    if (!booted) { console.error('应用未启动，探针终止'); app.exit(1); }
    await sleep(1500);

    // 回到 dashboard 作为统一起点
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(400);

    // ── P0. 九页渲染冒烟：每页可见宿主有内容、无错误弹窗 ──
    const pageKeys = ['dashboard','tasks','dag','agents','gates','artifacts','events','providers','settings'];
    for (const pk of pageKeys) {
      const res = await js(`(async () => {
        document.querySelector('.nav-item[data-nav="${pk}"]')?.click();
        await new Promise(r=>setTimeout(r,400));
        const dv=document.querySelector('#dashboard-view'), pv=document.querySelector('#placeholder-view');
        const host = (dv && !dv.classList.contains('hidden')) ? dv : pv;
        const nodes = host ? host.querySelectorAll('*').length : 0;
        const errToast = !!document.querySelector("[class*='toast'][class*='error']");
        return JSON.stringify({ nodes, errToast });
      })()`);
      let pd = {}; try { pd = JSON.parse(res); } catch {}
      check(`P0. 页面[${pk}]渲染有内容、无错误弹窗`, Number(pd.nodes) > 0 && pd.errToast === false,
        `nodes=${pd.nodes} errToast=${pd.errToast}`);
    }
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(400);

    // ── P1. 数字快捷键 8/9 与侧边栏视觉顺序一致性 ──
    await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'8',bubbles:true}))`);
    await sleep(250);
    const nav8 = await nav();
    await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'9',bubbles:true}))`);
    await sleep(250);
    const nav9 = await nav();
    check('P1. 快捷键 8→供应商、9→设置（对齐侧边栏顺序）', nav8 === 'providers' && nav9 === 'settings',
      `8→${nav8}（期望 providers）；9→${nav9}（期望 settings）`);
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(300);

    // ── P2. 模态打开时数字键不应切换底层页面 ──
    let p2 = await js(`(() => {
      document.querySelector('#btn-new-run')?.click();
      const modalOpen = !document.querySelector('#modal').classList.contains('hidden');
      window.dispatchEvent(new KeyboardEvent('keydown',{key:'4',bubbles:true}));
      return modalOpen;
    })()`);
    await sleep(250);
    const p2nav = await nav();
    check('P2. 新建运行模态打开时按数字键不切换页面', p2 === true && p2nav === 'dashboard',
      `modalOpen=${p2} nav=${p2nav}（期望 dashboard）`);
    await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})`);
    await sleep(300);

    // ── P3. 运行下拉：打开→再点工作区应关闭（toggle） ──
    let p3 = await js(`(() => {
      const ws = document.querySelector('#tb-workspace');
      ws.click();
      const open = !document.querySelector('#run-dropdown').classList.contains('hidden');
      ws.click();
      const closed = document.querySelector('#run-dropdown').classList.contains('hidden');
      return JSON.stringify({ open, closed });
    })()`);
    let p3d = {}; try { p3d = JSON.parse(p3); } catch {}
    check('P3. 点工作区打开下拉、再点关闭', p3d.open && p3d.closed, JSON.stringify(p3d));

    // ── P4. 下拉打开后点击页面其他处应关闭（outside click） ──
    let p4 = await js(`(() => {
      document.querySelector('#tb-workspace').click();
      const open = !document.querySelector('#run-dropdown').classList.contains('hidden');
      document.body.dispatchEvent(new MouseEvent('click',{bubbles:true}));
      const closed = document.querySelector('#run-dropdown').classList.contains('hidden');
      return JSON.stringify({ open, closed });
    })()`);
    let p4d = {}; try { p4d = JSON.parse(p4); } catch {}
    check('P4. 下拉外点页面其他处关闭', p4d.open && p4d.closed, JSON.stringify(p4d));

    // ── P5. 新建运行：空目标且未勾 Demo 时启动被拦截 ──
    let p5 = await js(`(() => {
      document.querySelector('#btn-new-run')?.click();
      const demo = document.querySelector('#nr-demo'); if (demo) demo.checked = false;
      const goal = document.querySelector('#nr-goal'); goal.value = ''; goal.disabled = false;
      document.querySelector('#nr-start').click();
      const stillOpen = !document.querySelector('#modal').classList.contains('hidden');
      const err = (document.querySelector('#nr-goal-error')?.textContent || '');
      return JSON.stringify({ stillOpen, err });
    })()`);
    let p5d = {}; try { p5d = JSON.parse(p5); } catch {}
    check('P5. 空目标启动被行内拦截、模态不关闭', p5d.stillOpen && p5d.err.length > 0, JSON.stringify(p5d));
    await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})`);
    await sleep(300);

    // ── P6. Mock 勾选联动模型选择区置灰/恢复 ──
    let p6 = await js(`(() => {
      document.querySelector('#btn-new-run')?.click();
      const mock = document.querySelector('#nr-mock');
      mock.checked = true; mock.dispatchEvent(new Event('change',{bubbles:true}));
      const provDisabled = document.querySelector('#nr-provider').disabled;
      mock.checked = false; mock.dispatchEvent(new Event('change',{bubbles:true}));
      const provEnabled = !document.querySelector('#nr-provider').disabled;
      return JSON.stringify({ provDisabled, provEnabled });
    })()`);
    let p6d = {}; try { p6d = JSON.parse(p6); } catch {}
    check('P6. Mock 勾选模型区置灰、取消恢复', p6d.provDisabled && p6d.provEnabled, JSON.stringify(p6d));
    await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})`);
    await sleep(300);

    // ── P7. Demo 勾选目标框置灰/恢复 ──
    let p7 = await js(`(() => {
      document.querySelector('#btn-new-run')?.click();
      const demo = document.querySelector('#nr-demo');
      demo.checked = true; demo.dispatchEvent(new Event('change',{bubbles:true}));
      const goalDisabled = document.querySelector('#nr-goal').disabled;
      demo.checked = false; demo.dispatchEvent(new Event('change',{bubbles:true}));
      const goalEnabled = !document.querySelector('#nr-goal').disabled;
      return JSON.stringify({ goalDisabled, goalEnabled });
    })()`);
    let p7d = {}; try { p7d = JSON.parse(p7); } catch {}
    check('P7. Demo 勾选目标框置灰、取消恢复', p7d.goalDisabled && p7d.goalEnabled, JSON.stringify(p7d));
    await js(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})`);
    await sleep(300);

    // ── P8. 快速连点导航不产生多个 active ──
    let p8b = await js(`(() => {
      const b = document.querySelector('.nav-item[data-nav="agents"]');
      for (let i=0;i<5;i++) b.click();
      return document.querySelectorAll('.nav-item.active').length;
    })()`);
    check('P8. 连点导航仅 1 个 active', Number(p8b) === 1, `active=${p8b}`);
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(300);

    // ── P9. DAG 缩放：放大/缩小/重置，且不出现非正缩放 ──
    let p9 = await js(`(() => {
      const zin = document.querySelector('#dag-zoom-in');
      const zout = document.querySelector('#dag-zoom-out');
      for (let i=0;i<3;i++) zin.click();
      const afterIn = document.querySelector('#dag-zoom-label').textContent;
      for (let i=0;i<20;i++) zout.click();
      const afterManyOut = document.querySelector('#dag-zoom-label').textContent;
      document.querySelector('#dag-reset').click();
      const afterReset = document.querySelector('#dag-zoom-label').textContent;
      return JSON.stringify({ afterIn, afterManyOut, afterReset });
    })()`);
    let p9d = {}; try { p9d = JSON.parse(p9); } catch {}
    const inUp = /\d+/.exec(p9d.afterIn) && parseInt(/\d+/.exec(p9d.afterIn)[0]) > 100;
    const outMin = /\d+/.exec(p9d.afterManyOut) ? parseInt(/\d+/.exec(p9d.afterManyOut)[0]) : -1;
    const outOk = outMin > 0;
    const resetOk = p9d.afterReset.indexOf('100') >= 0;
    check('P9. DAG 放大生效、连缩不出现非正缩放、重置回100%', inUp && outOk && resetOk,
      JSON.stringify(p9d));

    // ── P10. 任务详情：点节点打开、点遮罩关闭（关闭有 220ms 动画延迟） ──
    let p10open = await js(`(() => {
      const node = document.querySelector('.dag-node, [data-task-id]');
      if (!node) return false;
      node.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
      return !document.querySelector('#detail-panel').classList.contains('hidden');
    })()`);
    await js(`document.querySelector('#detail-mask').click()`);
    await sleep(400);
    const p10closed = await js(`document.querySelector('#detail-panel').classList.contains('hidden')`);
    check('P10. 点任务打开详情、点遮罩关闭', p10open === true && p10closed === true,
      `opened=${p10open} closed=${p10closed}`);

    // ── P11. 事件页筛选：只保留某一类型后列表不含其它类型 ──
    await js(`document.querySelector('.nav-item[data-nav="events"]')?.click()`);
    await sleep(600);
    let p11 = await js(`(() => {
      const btns = Array.from(document.querySelectorAll('.ev-filter-btn'));
      const first = btns[0]; const keepType = first.dataset.type;
      for (const b of btns.slice(1)) { if (b.classList.contains('active')) b.click(); }
      const rows = Array.from(document.querySelectorAll('.ev-full-type')).map(x=>x.textContent);
      const allMatch = rows.every(t => t === keepType);
      for (const b of btns) { if (!b.classList.contains('active')) b.click(); }
      return JSON.stringify({ keepType, rows: rows.length, allMatch });
    })()`);
    let p11d = {}; try { p11d = JSON.parse(p11); } catch {}
    check('P11. 事件单类型筛选隔离正确', p11d.allMatch && Number(p11d.rows) > 0, JSON.stringify(p11d));

    // ── P12. 供应商搜索：无结果态 + 清空恢复（200ms 防抖，重渲染列表） ──
    await js(`document.querySelector('.nav-item[data-nav="providers"]')?.click()`);
    await sleep(700);
    const p12before = await js(`document.querySelectorAll('#prov-list .prov-card').length`);
    await js(`(() => { const s=document.querySelector('#prov-search'); s.value='zzz-no-match-xyz'; s.dispatchEvent(new Event('input',{bubbles:true})); })()`);
    await sleep(400);
    const p12none = await js(`JSON.stringify({cards:document.querySelectorAll('#prov-list .prov-card').length, empty:document.querySelectorAll('#prov-list .prov-empty').length})`);
    await js(`(() => { const s=document.querySelector('#prov-search'); s.value=''; s.dispatchEvent(new Event('input',{bubbles:true})); })()`);
    await sleep(400);
    const p12clear = await js(`document.querySelectorAll('#prov-list .prov-card').length`);
    let p12d = { before: p12before }; try { p12d = Object.assign(p12d, JSON.parse(p12none), { afterClear: p12clear }); } catch {}
    check('P12. 供应商搜索无结果、清空恢复',
      Number(p12before) > 1 && p12d.cards === 0 && p12d.empty === 1 && Number(p12d.afterClear) === Number(p12before),
      JSON.stringify(p12d));

    // ── P13. 供应商分类：单选 active，点「全部」恢复 ──
    let p13 = await js(`(() => {
      const cats = Array.from(document.querySelectorAll('#prov-cat-bar .prov-cat-btn'));
      const allBtn = cats.find(b => b.dataset.cat === 'all');
      const target = cats.find(b => b.dataset.cat && b.dataset.cat !== 'all');
      if (!target) return JSON.stringify({ hasCats:false });
      target.click();
      const activeCats = cats.filter(b => b.classList.contains('active')).length;
      let restored = null;
      if (allBtn) { allBtn.click(); restored = document.querySelectorAll('#prov-list .prov-card').length; }
      return JSON.stringify({ hasCats:true, activeCats, restored });
    })()`);
    let p13d = {}; try { p13d = JSON.parse(p13); } catch {}
    check('P13. 供应商分类单选、「全部」恢复', p13d.hasCats && p13d.activeCats === 1 && p13d.restored != null && p13d.restored > 1,
      JSON.stringify(p13d));

    // ── P14. 主题模式按 light→dark→system 循环、持久化，3 击回到起点（走真实 IPC） ──
    let p14 = await js(`(async () => {
      const get = async () => (await window.studio.getSettings()).settings.theme || 'dark';
      const start = await get();
      const seen = [start];
      for (let i=0;i<3;i++){
        document.querySelector('#btn-theme').click();
        await new Promise(r=>setTimeout(r,350));
        seen.push(await get());
      }
      return JSON.stringify({ start, seen });
    })()`);
    let p14d = {}; try { p14d = JSON.parse(p14); } catch {}
    const TORDER = ['light','dark','system'];
    let cycleOk = Array.isArray(p14d.seen) && p14d.seen.length === 4 && p14d.seen[3] === p14d.start;
    if (cycleOk) {
      for (let i=0;i<3;i++) {
        const a = p14d.seen[i], b = p14d.seen[i+1];
        if (TORDER[(TORDER.indexOf(a)+1)%3] !== b) { cycleOk = false; }
      }
    }
    check('P14. 主题模式按序循环并持久化、3 击回起点', cycleOk, JSON.stringify(p14d));

    // ── P15. 设置：负数预算保存后应被夹到 0（走真实保存按钮 + getSettings 回读） ──
    await js(`document.querySelector('.nav-item[data-nav="settings"]')?.click()`);
    await sleep(700);
    let p15 = await js(`(async () => {
      const before = (await window.studio.getSettings()).settings.maxCostUsd;
      const mc = document.querySelector('#set-maxcost');
      mc.value = '-5';
      document.querySelector('.page-head .btn-primary').click();
      await new Promise(r=>setTimeout(r,600));
      const saved = (await window.studio.getSettings()).settings.maxCostUsd;
      await window.studio.setSettings({ maxCostUsd: before });   // 还原
      return JSON.stringify({ before, saved });
    })()`);
    let p15d = {}; try { p15d = JSON.parse(p15); } catch {}
    check('P15. 负预算保存后被夹到 0', Number(p15d.saved) === 0,
      `saved=${p15d.saved}（期望 0）；原值=${p15d.before}`);

    // ── 汇总 ──
    const failed = checks.filter(c => !c.pass);
    const summary = { when: new Date().toISOString(), allPass: failed.length === 0,
      total: checks.length, failedCount: failed.length, checks };
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(summary, null, 2));
    await shot('probe-final');
    console.log(`\n探针完成：${checks.length - failed.length}/${checks.length} 通过，失败 ${failed.length} 项 → ${OUT}`);
    app.exit(0);
  })();
}

module.exports = { run };
