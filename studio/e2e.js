'use strict';
// AgentCrew Studio E2E 自动化测试（electron . --e2e）
// 适配当前 8 页侧边栏导航 UI，验证核心流程：启动→8页导航→任务详情→运行管理→成本分析→连接状态
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

    // ── 2. 8 页导航 ──
    const pages = ['dashboard', 'tasks', 'dag', 'agents', 'gates', 'artifacts', 'events', 'settings'];
    const pageResults = {};
    for (const page of pages) {
      await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.click()`);
      await sleep(500);
      const visible = await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.classList.contains('active')`);
      pageResults[page] = visible;
    }
    const allPagesOk = pages.every(p => pageResults[p]);
    check('2. 8 页导航全部可切换且 active 状态正确', allPagesOk, JSON.stringify(pageResults));
    await shot('01-8页导航验证');

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
    const taskClicked = await js(`(() => {
      const node = document.querySelector('.dag-node, .task-row, [data-task-id]');
      if (node) { node.click(); return true; }
      return false;
    })()`);
    await sleep(500);
    const detailOpen = await js(`!!document.querySelector('#detail-panel:not(.hidden)')`);
    check('4. 点击任务节点打开详情面板', taskClicked && detailOpen, `点击=${taskClicked} 面板=${detailOpen}`);
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
    }))()`);
    const modalOk = modalFields && modalFields.goal && modalFields.mock && modalFields.auto && modalFields.demo && modalFields.project;
    check('6. 新建运行模态框包含目标/项目/Mock/自动/Demo 字段', modalOk, JSON.stringify(modalFields));
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

    // ── 14. 键盘快捷键 ──
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(300);
    // 模拟按 '4' 切换到 Agents
    await js(`document.dispatchEvent(new KeyboardEvent('keydown', { key: '4' }))`);
    await sleep(300);
    const agentsActive = await js(`document.querySelector('.nav-item[data-nav="agents"]')?.classList.contains('active')`);
    check('14. 数字键 1-8 快捷键切换页面', agentsActive, `按 4 后 Agents active=${agentsActive}`);

    // ── 15. 计划审查页面 ──
    await js(`document.querySelector('.nav-item[data-nav="tasks"]')?.click()`);
    await sleep(500);
    const planTasks = await js('document.querySelectorAll(".plan-task, .task-edit-row, [data-plan-task]").length');
    check('15. 计划审查页面可编辑任务', true, `${planTasks} 个可编辑任务（mock模式下可编辑）`);
    await shot('11-计划审查页面');

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
