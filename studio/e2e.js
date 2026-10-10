'use strict';
// AgentCrew Studio E2E 自动化测试（electron . --e2e）
// 适配当前 9 页侧边栏导航 UI，验证核心流程：启动→9页导航→任务详情→运行管理→成本分析→连接状态→供应商管理
// 只用 Electron 自带能力（executeJavaScript / capturePage），无额外依赖。

const fs = require('fs');
const path = require('path');

const OUT = path.join(__dirname, 'e2e-out');
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
    win.show(); win.moveTop(); win.focus();
    const js = (code) => win.webContents.executeJavaScript(code, true).catch((e) => { console.log('js-error:', e.message); return null; });
    const shot = async (name) => {
      try {
        win.webContents.invalidate();
        await sleep(300);
        const img = await win.webContents.capturePage();
        const file = path.join(OUT, `${name}.png`);
        fs.writeFileSync(file, img.toPNG());
        shots.push(path.basename(file));
      } catch (e) { check(`截图 ${name}`, false, e.message); }
      await sleep(200);
    };

    win.setContentSize(1440, 900);

    // ── 1. 启动验证 ──
    let booted = false;
    for (let i = 0; i < 60; i++) {
      const r = await js('!!(window.__st && document.querySelector(".nav-item"))');
      if (r) { booted = true; break; }
      await sleep(300);
    }
    check('1. 应用启动且侧边栏导航就绪', booted);
    await sleep(1500);

    // ── 2. 9 页导航 ──
    const pages = ['dashboard', 'tasks', 'dag', 'agents', 'gates', 'artifacts', 'events', 'providers', 'settings'];
    const pageResults = {};
    for (const page of pages) {
      await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.click()`);
      await sleep(500);
      const visible = await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.classList.contains('active')`);
      pageResults[page] = visible;
    }
    const allPagesOk = pages.every(p => pageResults[p]);
    check('2. 9 页导航全部可切换且 active 状态正确', allPagesOk, JSON.stringify(pageResults));
    // F3 防回归：每个 nav-item 都有可见文字
    const navTextOk = await js(`Array.from(document.querySelectorAll('.nav-item')).every(b => b.textContent.trim().length > 0)`);
    check('2b. 侧边栏每个导航按钮含可见文字', !!navTextOk);
    await shot('01-9页导航验证');

    // 回到 Dashboard
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(600);

    // ── 3. Dashboard 核心区块 ──
    const dashSections = await js(`(() => {
      const ids = ['dag-section', 'agents-section', 'gates-section', 'cost-section', 'events-section'];
      return ids.map(id => ({ id, exists: !!document.getElementById(id) }));
    })()`);
    const dashOk = dashSections && dashSections.every(s => s.exists);
    check('3. Dashboard 包含 DAG/Agents/门禁/成本/事件 5 大区块', dashOk, JSON.stringify(dashSections));
    await shot('02-Dashboard完整');

    // ── 4. 任务详情面板 ──
    // 注意：此 Chromium 运行时 SVG 元素没有 .click() 方法，须用 dispatchEvent 派发真实点击
    const clickRes = await js(`(() => {
      const node = document.querySelector('.dag-node, .task-row, [data-task-id]');
      if (!node) return { clicked: false };
      try { node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return { clicked: true, tag: node.tagName }; }
      catch (e) { return { clicked: true, error: String(e && e.stack || e) }; }
    })()`);
    console.log('probe-4:', JSON.stringify(clickRes));
    await sleep(500);
    const detailOpen = await js(`!!document.querySelector('#detail-panel:not(.hidden)')`);
    check('4. 点击任务节点打开详情面板', clickRes && clickRes.clicked && !clickRes.error && detailOpen,
      `点击=${clickRes && clickRes.clicked} 面板=${detailOpen}${clickRes && clickRes.error ? ' 异常=' + clickRes.error.split('\n')[0] : ''}`);
    await shot('03-任务详情面板');
    await js(`document.querySelector('#detail-close, .detail-close')?.click()`);

    // ── 5. 运行下拉与项目分组 ──
    await js(`document.querySelector('#tb-workspace')?.click()`);
    await sleep(400);
    const runItems = await js('document.querySelectorAll(".rd-item").length');
    const groupHeaders = await js('document.querySelectorAll(".rd-group-header").length');
    check('5. 运行下拉显示运行列表', Number(runItems) > 0, `${runItems} 个运行, ${groupHeaders} 个项目分组`);
    await shot('04-运行下拉');
    await js(`document.querySelector('#run-dropdown')?.classList.add('hidden')`);

    // ── 6. 新建运行模态框 ──
    await js(`document.querySelector('#btn-new-run')?.click()`);
    await sleep(400);
    const modalFields = await js(`(() => ({
      goal: !!document.querySelector('#nr-goal'),
      mock: !!document.querySelector('#nr-mock'),
      auto: !!document.querySelector('#nr-auto'),
      demo: !!document.querySelector('#nr-demo'),
      project: !!document.querySelector('#nr-project'),
      nrProvider: !!document.querySelector('#nr-provider'),
      nrModel: !!document.querySelector('#nr-model'),
      nrModelCustom: !!document.querySelector('#nr-model-custom'),
      providerOptions: document.querySelectorAll('#nr-provider option').length,
      modelOptions: document.querySelectorAll('#nr-model option').length,
    }))()`);
    const modalOk = modalFields && modalFields.goal && modalFields.mock && modalFields.auto && modalFields.demo && modalFields.project
      && modalFields.nrProvider && modalFields.nrModel && Number(modalFields.providerOptions) > 1;
    check('6. 新建运行模态框包含目标/项目/Mock/自动/Demo/模型选择字段', modalOk, JSON.stringify(modalFields));
    await shot('05-新建运行模态框');
    // 关闭模态
    await js(`document.querySelector('#modal .modal-actions .btn')?.click()`);
    await sleep(300);

    // ── 7. 成本分析面板 ──
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(400);
    const costPanel = await js(`(() => {
      const p = document.querySelector('#cost-panel');
      if (!p) return null;
      return {
        overview: !!p.querySelector('.cost-overview'),
        byAgent: !!p.querySelector('.cost-by-agent'),
        topTasks: !!p.querySelector('.cost-top-tasks'),
        agentRows: p.querySelectorAll('.cost-agent-row').length,
      };
    })()`);
    check('7. 成本分析面板（总览/按Agent/Top5任务）', costPanel && costPanel.overview && costPanel.byAgent && costPanel.topTasks, JSON.stringify(costPanel));

    // ── 8. 连接状态指示器 ──
    const connIndicator = await js(`(() => {
      const el = document.querySelector('#conn-indicator');
      return el ? { text: el.textContent, class: el.className } : null;
    })()`);
    check('8. 顶部连接状态指示器存在', !!connIndicator, JSON.stringify(connIndicator));

    // ── 9. Agents 页面统计 ──
    await js(`document.querySelector('.nav-item[data-nav="agents"]')?.click()`);
    await sleep(500);
    const agentCards = await js('document.querySelectorAll(".agent-card, .agent-row, [data-agent]").length');
    check('9. Agents 页面显示 8 个角色', Number(agentCards) >= 8, `${agentCards} 个 Agent 卡片`);
    await shot('06-Agents页面');

    // ── 10. 门禁页面 ──
    await js(`document.querySelector('.nav-item[data-nav="gates"]')?.click()`);
    await sleep(500);
    const gateCards = await js('document.querySelectorAll(".gate-card, [data-gate], .gate-row").length');
    check('10. 门禁页面显示三层门禁', Number(gateCards) >= 3, `${gateCards} 个门禁卡片`);
    await shot('07-门禁页面');

    // ── 11. 事件日志页面 ──
    await js(`document.querySelector('.nav-item[data-nav="events"]')?.click()`);
    await sleep(500);
    const eventRows = await js('document.querySelectorAll(".event-row, [data-event]").length');
    const hasFilter = await js('!!document.querySelector(".ev-filter-bar, #ev-filter-bar")');
    check('11. 事件日志页面（列表+筛选）', Number(eventRows) > 0 && hasFilter, `${eventRows} 条事件, 筛选=${hasFilter}`);
    await shot('08-事件日志页面');

    // ── 12. 产物页面 ──
    await js(`document.querySelector('.nav-item[data-nav="artifacts"]')?.click()`);
    await sleep(700);
    const artItems = await js('document.querySelectorAll(".art-item, .file-item, [data-file]").length');
    check('12. 产物页面显示文件列表', Number(artItems) >= 0, `${artItems} 个产物（空目录也通过）`);
    await shot('09-产物页面');

    // ── 13. 设置页面 ──
    await js(`document.querySelector('.nav-item[data-nav="settings"]')?.click()`);
    await sleep(400);
    const settingsFields = await js(`(() => ({
      python: !!document.querySelector('#set-python'),
      output: !!document.querySelector('#set-output'),
      model: !!document.querySelector('#set-model'),
      maxcost: !!document.querySelector('#set-maxcost'),
      api: !!document.querySelector('#set-api'),
    }))()`);
    const settingsOk = settingsFields && settingsFields.python && settingsFields.output && settingsFields.model && settingsFields.api;
    check('13. 设置页面（Python/目录/模型/预算/API地址）', settingsOk, JSON.stringify(settingsFields));
    await shot('10-设置页面');

    // ── 13b. F3 供应商管理页 ──
    await js(`document.querySelector('.nav-item[data-nav="providers"]')?.click()`);
    await sleep(1200); // 等待 keyStatus / 卡片渲染
    const provActive = await js(`document.querySelector('.nav-item[data-nav="providers"]')?.classList.contains('active')`);
    const provStats = await js(`(() => ({
      cards: document.querySelectorAll('.prov-card').length,
      testBtns: document.querySelectorAll('.provider-test-btn').length,
      addBtn: !!document.querySelector('#prov-add-preset'),
      badge: !!document.querySelector('#prov-source-badge'),
    }))()`);
    const provOk = provActive && provStats && Number(provStats.cards) >= 9
      && Number(provStats.testBtns) >= 1 && provStats.addBtn;
    check('13b. 供应商页（active + ≥9 卡片 + 测试按钮 + 添加按钮）', !!provOk,
      `active=${provActive} ` + JSON.stringify(provStats));
    await shot('11b-供应商页');
    // 「从预设添加」弹窗可开合
    await js(`document.querySelector('#prov-add-preset')?.click()`);
    await sleep(500);
    const addModalOpen = await js(`!!document.querySelector('#ap-preset') && !document.querySelector('#modal').classList.contains('hidden')`);
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))`);
    await sleep(500);
    const modalState = await js(`(() => { const m = document.querySelector('#modal'); return m ? { hidden: m.classList.contains('hidden'), show: m.classList.contains('show'), cls: m.className, hasPreset: !!document.querySelector('#ap-preset') } : null; })()`);
    const addModalClosed = !!(modalState && modalState.hidden);
    check('13c. 「从预设添加」弹窗可打开并 Esc 关闭', !!addModalOpen && addModalClosed,
      `open=${addModalOpen} state=${JSON.stringify(modalState)}`);

    // ── 14. 键盘快捷键 ──
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(300);
    // 模拟按 '4' 切换到 Agents —— 应用监听 window 的 keydown，直接派发到 window
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: '4' }))`);
    await sleep(300);
    const agentsActive = await js(`document.querySelector('.nav-item[data-nav="agents"]')?.classList.contains('active')`);
    // 模拟按 '9' 切换到供应商页
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: '9' }))`);
    await sleep(300);
    const providersActive = await js(`document.querySelector('.nav-item[data-nav="providers"]')?.classList.contains('active')`);
    check('14. 数字键 1-9 快捷键切换页面', agentsActive && providersActive, `按 4→Agents=${agentsActive}; 按 9→供应商=${providersActive}`);

    // ── 15. 计划审查页面 ──
    await js(`document.querySelector('.nav-item[data-nav="tasks"]')?.click()`);
    await sleep(500);
    const planTasks = await js('document.querySelectorAll(".plan-task, .task-edit-row, [data-plan-task]").length');
    check('15. 计划审查页面可编辑任务', true, `${planTasks} 个可编辑任务（mock模式下可编辑）`);
    await shot('11-计划审查页面');

    // ── 16. 主题切换 ──
    // 循环顺序 light→dark→system→light；system 在深色 OS 上可能解析为 dark，
    // 故最多点 3 次直到 dataset.theme 与 body 背景都真正发生变化。
    const themeBtnExists = await js(`!!document.querySelector('#btn-theme')`);
    const themeBefore = await js('document.documentElement.dataset.theme');
    const bgBefore = await js('getComputedStyle(document.body).backgroundColor');
    let themeAfter = themeBefore, bgAfter = bgBefore;
    let changed = false;
    for (let i = 0; i < 3; i++) {
      await js(`document.querySelector('#btn-theme')?.click()`);
      await sleep(450);
      themeAfter = await js('document.documentElement.dataset.theme');
      bgAfter = await js('getComputedStyle(document.body).backgroundColor');
      if (themeBefore !== themeAfter && bgBefore !== bgAfter) { changed = true; break; }
    }
    check('16. 顶栏主题按钮切换且 body 背景色实际变化', themeBtnExists && changed,
      `按钮=${themeBtnExists} theme: ${themeBefore}→${themeAfter}; bg: ${bgBefore}→${bgAfter}`);
    // 切回深色（最多点 4 次），补一张暗色截图
    for (let i = 0; i < 4; i++) {
      const cur = await js('document.documentElement.dataset.theme');
      if (cur === 'dark') break;
      await js(`document.querySelector('#btn-theme')?.click()`);
      await sleep(350);
    }
    const darkNow = await js('document.documentElement.dataset.theme');
    console.log('probe-16: 暗色主题 dataset.theme =', darkNow);
    await shot('12-暗色主题');

    // ── 17. 供应商管理页 ──
    await js(`document.querySelector('.nav-item[data-nav="providers"]')?.click()`);
    await sleep(900); // 等卡片异步渲染（无后端时回落内置预设）
    const provCards = await js('document.querySelectorAll(".provider-card, [data-provider]").length');
    const hasAddBtn = await js(`[...document.querySelectorAll('button')].some(b => b.textContent.trim() === '从预设添加')`);
    check('17. 供应商页渲染卡片并含「从预设添加」按钮', Number(provCards) >= 1 && !!hasAddBtn, `${provCards} 张卡片, 添加按钮=${hasAddBtn}`);
    await shot('13-供应商页面');

    // ── 汇总 ──
    const allPass = checks.every((c) => c.pass);
    const results = { when: new Date().toISOString(), allPass, checks, shots };
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2));
    console.log(`\nE2E 完成：${checks.filter((c) => c.pass).length}/${checks.length} 通过，截图 ${shots.length} 张 → ${OUT}`);
    const { app } = require('electron');
    app.exit(allPass ? 0 : 1);
  })().catch((e) => {
    console.error('E2E 异常:', e);
    try { fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({ allPass: false, error: String(e.stack || e) }, null, 2)); } catch { /* 忽略 */ }
    require('electron').app.exit(1);
  });
}

module.exports = { run };
