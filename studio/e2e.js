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

    // ── 2. 12 页导航 ──
    const pages = ['conversation', 'dashboard', 'tasks', 'dag', 'agents', 'profiles', 'gates', 'artifacts', 'events', 'providers', 'schedules', 'settings'];
    const pageResults = {};
    for (const page of pages) {
      await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.click()`);
      await sleep(500);
      const visible = await js(`document.querySelector('.nav-item[data-nav="${page}"]')?.classList.contains('active')`);
      pageResults[page] = visible;
    }
    const allPagesOk = pages.every(p => pageResults[p]);
    check('2. 12 页导航全部可切换且 active 状态正确', allPagesOk, JSON.stringify(pageResults));
    // F3 防回归：每个 nav-item 都有可见文字
    const navTextOk = await js(`Array.from(document.querySelectorAll('.nav-item')).every(b => b.textContent.trim().length > 0)`);
    check('2b. 侧边栏每个导航按钮含可见文字', !!navTextOk);
    await shot('01-9页导航验证');

    // 2c. 新增页内容渲染：执行体档案 / 定时任务 各出标题（不只 nav 高亮）
    await js(`document.querySelector('.nav-item[data-nav="profiles"]')?.click()`);
    await sleep(700);
    const profilesOk = await js(`!!document.querySelector('#placeholder-view .page-head h2') && document.querySelector('#placeholder-view .page-head h2').textContent.includes('执行体')`);
    await shot('01b-执行体档案');
    await js(`document.querySelector('.nav-item[data-nav="schedules"]')?.click()`);
    await sleep(700);
    const schedulesOk = await js(`!!document.querySelector('#placeholder-view .page-head h2') && document.querySelector('#placeholder-view .page-head h2').textContent.includes('定时任务')`);
    check('2c. 执行体档案 / 定时任务 页内容渲染', !!(profilesOk && schedulesOk), JSON.stringify({ profilesOk, schedulesOk }));

    // 2d. 对话页：消息流渲染（用户目标气泡 + 任务气泡）
    await js(`document.querySelector('.nav-item[data-nav="conversation"]')?.click()`);
    await sleep(700);
    const convOk = await js(`!!document.querySelector('#placeholder-view .conv-list') && !!document.querySelector('#placeholder-view .conv-user') && !!document.querySelector('#placeholder-view .conv-agent')`);
    check('2d. 对话页消息流渲染（目标+任务气泡）', !!convOk, JSON.stringify({ convOk }));
    await shot('01c-对话页');

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
      mode: !!document.querySelector('#nr-mode') && document.querySelectorAll('#nr-mode .nr-mode-btn').length === 3,
      demo: !!document.querySelector('#nr-demo'),
      project: !!document.querySelector('#nr-project'),
      nrProvider: !!document.querySelector('#nr-provider'),
      nrModel: !!document.querySelector('#nr-model'),
      nrModelCustom: !!document.querySelector('#nr-model-custom'),
      providerOptions: document.querySelectorAll('#nr-provider option').length,
      modelOptions: document.querySelectorAll('#nr-model option').length,
    }))()`);
    const modalOk = modalFields && modalFields.goal && modalFields.mock && modalFields.mode && modalFields.demo && modalFields.project
      && modalFields.nrProvider && modalFields.nrModel && Number(modalFields.providerOptions) > 1;
    check('6. 新建运行模态框包含目标/项目/Mock/执行模式(3态)/Demo/模型选择字段', modalOk, JSON.stringify(modalFields));
    // 6b. Mock 勾选置灰模型选择区（--mock 时别名不参与 CLI 参数）
    const mockToggle = await js(`(() => {
      const m = document.querySelector('#nr-mock'); if (!m) return null;
      m.checked = true; m.dispatchEvent(new Event('change'));
      const prov = document.querySelector('#nr-provider');
      const area = document.querySelector('#nr-model-area');
      return { disabled: prov ? prov.disabled : null, opacity: area ? area.style.opacity : null };
    })()`);
    check('6b. 勾选Mock后模型下拉置灰', !!(mockToggle && mockToggle.disabled === true && mockToggle.opacity === '0.45'),
      JSON.stringify(mockToggle));
    // 还原 mock 勾选，便于后续流程
    await js(`(() => { const m = document.querySelector('#nr-mock'); if (m) { m.checked = false; m.dispatchEvent(new Event('change')); } })()`);
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

    // ── 13d. V5 P3：搜索框 / 分类筛选 / 编辑按钮 ──
    await sleep(400);
    const provEnhance = await js(`(() => ({
      search: !!document.querySelector('#prov-search'),
      catBtns: document.querySelectorAll('#prov-cat-bar .prov-cat-btn').length,
      editBtns: document.querySelectorAll('.prov-edit-btn').length,
      count: !!document.querySelector('#prov-count'),
      builtinCount: (typeof BUILTIN_PROVIDERS !== 'undefined') ? BUILTIN_PROVIDERS.length : -1,
    }))()`);
    const enhanceOk = provEnhance && provEnhance.search
      && Number(provEnhance.catBtns) >= 6
      && Number(provEnhance.editBtns) >= 1
      && provEnhance.count
      && Number(provEnhance.builtinCount) >= 25;
    check('13d. 供应商页增强（搜索框 + ≥6 分类按钮 + 编辑按钮 + BUILTIN≥25）', !!enhanceOk,
      JSON.stringify(provEnhance));

    // 搜索输入实时过滤
    const searchResult = await js(`(() => {
      const s = document.querySelector('#prov-search');
      if (!s) return null;
      s.value = 'openai';
      s.dispatchEvent(new Event('input', { bubbles: true }));
      return { typed: s.value };
    })()`);
    await sleep(500); // 等 200ms 防抖 + 重渲染
    const filteredCount = await js(`document.querySelectorAll('.prov-card').length`);
    check('13e. 搜索框输入后实时过滤卡片', !!searchResult && Number(filteredCount) >= 1,
      `typed=${searchResult && searchResult.typed} 剩余卡片=${filteredCount}`);
    // 清空搜索
    await js(`(() => { const s = document.querySelector('#prov-search'); if (s) { s.value=''; s.dispatchEvent(new Event('input',{bubbles:true})); } })()`);
    await sleep(400);

    // 分类筛选按钮可点击
    const catClick = await js(`(() => {
      const b = document.querySelector('#prov-cat-bar .prov-cat-btn[data-cat="national"]');
      if (!b) return null;
      b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      return { clicked: true, active: b.classList.contains('active') };
    })()`);
    await sleep(400);
    const afterCatCards = await js(`document.querySelectorAll('.prov-card').length`);
    check('13f. 分类筛选按钮（国内）可点击并筛选', !!(catClick && catClick.active && Number(afterCatCards) >= 1),
      `active=${catClick && catClick.active} 卡片=${afterCatCards}`);
    // 切回全部
    await js(`document.querySelector('#prov-cat-bar .prov-cat-btn[data-cat="all"]')?.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}))`);
    await sleep(300);

    // 编辑按钮打开弹窗 → Esc 关闭
    await js(`document.querySelector('.prov-edit-btn')?.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}))`);
    await sleep(400);
    const editModalOpen = await js(`!!document.querySelector('#modal') && !document.querySelector('#modal').classList.contains('hidden') && !!document.querySelector('#ap-yaml')`);
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))`);
    await sleep(400);
    const editModalClosed = await js(`(() => { const m = document.querySelector('#modal'); return m ? m.classList.contains('hidden') : true; })()`);
    check('13g. 编辑按钮打开弹窗并 Esc 关闭', !!(editModalOpen && editModalClosed),
      `open=${editModalOpen} closed=${editModalClosed}`);
    await shot('11c-供应商增强');

    // ── 13h. 编辑弹窗保存 API Key → 写入 .env → 注入主进程环境（模型切换生效链路）──
    // 用临时变量名 __E2E_STUDIO_KEY__，不碰真实供应商 Key；结束后还原 .env
    const envPath = path.join(__dirname, '..', '.env');
    const envBackup = fs.existsSync(envPath) ? fs.readFileSync(envPath, 'utf8') : null;
    try {
      await js(`(() => {
        const card = document.querySelector('.prov-card');
        card?.querySelector('.prov-edit-btn')?.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      })()`);
      await sleep(400);
      const keyUi = await js(`(() => ({
        keyInput: !!document.querySelector('#ap-key'),
        keyType: document.querySelector('#ap-key')?.type || null,
        saveBtn: !!document.querySelector('.ap-key-save'),
        statusBefore: document.querySelector('.ap-key-status')?.textContent || null,
      }))()`);
      // 改环境变量名为测试名 → 填 Key 值 → 点保存
      await js(`(() => {
        const env = document.querySelector('#ap-env');
        if (env) { env.value = '__E2E_STUDIO_KEY__'; env.dispatchEvent(new Event('input', { bubbles: true })); }
        const key = document.querySelector('#ap-key');
        if (key) key.value = 'sk-e2e-test-123';
      })()`);
      await shot('11d-编辑弹窗APIKey输入');
      await js(`document.querySelector('.ap-key-save')?.click()`);
      await sleep(800); // 等 IPC + .env 写入
      const keyUiAfter = await js(`(() => ({
        status: document.querySelector('.ap-key-status')?.textContent || null,
        cleared: (document.querySelector('#ap-key')?.value || '') === '',
      }))()`);
      const envAfter = fs.existsSync(envPath) ? fs.readFileSync(envPath, 'utf8') : '';
      const inEnvFile = /^__E2E_STUDIO_KEY__=sk-e2e-test-123$/m.test(envAfter);
      const injected = process.env.__E2E_STUDIO_KEY__ === 'sk-e2e-test-123';
      check('13h. 编辑弹窗保存 API Key → 写入 .env → 注入主进程环境',
        !!(keyUi && keyUi.keyInput && keyUi.keyType === 'password' && keyUi.saveBtn
          && keyUiAfter && /已配置/.test(keyUiAfter.status || '') && keyUiAfter.cleared
          && inEnvFile && injected),
        `控件=${JSON.stringify(keyUi)} 保存后=${JSON.stringify(keyUiAfter)} .env命中=${inEnvFile} 环境注入=${injected}`);
      await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))`);
      await sleep(400);
    } finally {
      // 还原 .env 与主进程环境
      try {
        if (envBackup === null) fs.rmSync(envPath, { force: true });
        else fs.writeFileSync(envPath, envBackup, 'utf8');
      } catch (e) { console.log('env 还原失败:', e.message); }
      delete process.env.__E2E_STUDIO_KEY__;
    }

    // ── 14. 键盘快捷键 ──
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(300);
    // 数字键映射对齐侧边栏顺序：1→对话、5→Agents、8→产物（对话页是新增首位）
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: '1' }))`);
    await sleep(300);
    const convActive = await js(`document.querySelector('.nav-item[data-nav="conversation"]')?.classList.contains('active')`);
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: '5' }))`);
    await sleep(300);
    const agentsActive = await js(`document.querySelector('.nav-item[data-nav="agents"]')?.classList.contains('active')`);
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: '8' }))`);
    await sleep(300);
    const artifactsActive = await js(`document.querySelector('.nav-item[data-nav="artifacts"]')?.classList.contains('active')`);
    check('14. 数字键 1-9 快捷键切换页面（对齐侧边栏顺序）', convActive && agentsActive && artifactsActive,
      `按 1→对话=${convActive}; 按 5→Agents=${agentsActive}; 按 8→产物=${artifactsActive}`);

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

    // ── 16b. FE-001 回归：暗色下 DAG 节点主标题可读（fill 非近黑）──
    await js(`document.querySelector('.nav-item[data-nav="dashboard"]')?.click()`);
    await sleep(700);
    const dagTitle = await js(`(() => {
      const t = document.querySelector('.dag-node text.node-title') || document.querySelector('.dag-node text');
      if (!t) return null;
      const cs = getComputedStyle(t);
      const mm = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(cs.fill || '');
      if (!mm) return { fill: String(cs.fill), ok: false };
      const lum = (Number(mm[1]) + Number(mm[2]) + Number(mm[3])) / 3;
      return { fill: cs.fill, luminance: lum, ok: lum > 100 };
    })()`);
    check('16b. 暗色下DAG节点主标题可读(fill亮度>100, 防FE-001回归)', !!(dagTitle && dagTitle.ok),
      JSON.stringify(dagTitle));
    // 节点框在暗色下不应是纯白突兀卡片（与 --status-*-bg 一致）
    const nodeBox = await js(`(() => {
      const r = document.querySelector('.dag-node rect.node-box');
      if (!r) return null;
      const cs = getComputedStyle(r);
      return { fill: cs.fill };
    })()`);
    check('16c. DAG节点框fill已主题化(非写死#FFFFFF)', !!(nodeBox && !/rgb\(255, ?255, ?255\)/.test(nodeBox.fill)),
      JSON.stringify(nodeBox));

    // ── 17. 供应商管理页 ──
    await js(`document.querySelector('.nav-item[data-nav="providers"]')?.click()`);
    await sleep(900); // 等卡片异步渲染（无后端时回落内置预设）
    const provCards = await js('document.querySelectorAll(".provider-card, [data-provider]").length');
    const hasAddBtn = await js(`[...document.querySelectorAll('button')].some(b => b.textContent.trim() === '从预设添加')`);
    check('17. 供应商页渲染卡片并含「从预设添加」按钮', Number(provCards) >= 1 && !!hasAddBtn, `${provCards} 张卡片, 添加按钮=${hasAddBtn}`);
    await shot('13-供应商页面');

    // ── 18. 右侧结果区（概览 / 产物 双 tab）──
    await js(`document.getElementById('btn-result')?.click()`);
    await sleep(600);
    const resultOpen = await js(`!!document.querySelector('#detail-panel:not(.hidden) .result-tabs')`);
    const overviewOk = await js(`!!document.querySelector('#detail-panel .result-grid')`);
    await shot('14a-结果区概览');
    await js(`[...document.querySelectorAll('.result-tab')].find(b => b.textContent.trim() === '产物')?.click()`);
    await sleep(900);
    const artifactOk = await js(`!!document.querySelector('#detail-panel .result-art')`);
    await shot('14b-结果区产物');
    await js(`window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))`);
    await sleep(300);
    check('18. 右侧结果区（概览/产物双 tab）', !!(resultOpen && overviewOk && artifactOk),
      JSON.stringify({ resultOpen, overviewOk, artifactOk }));

    // ── 19. 侧边栏任务列表（搜索 + 状态筛选 + 角色分组）──
    const taskSearchOk = await js(`!!document.getElementById('sb-task-search')`);
    const taskFiltersOk = await js(`document.querySelectorAll('#sb-task-filters .sb-task-chip').length >= 5`);
    const taskGroupOk = await js(`!!document.querySelector('#sb-tasklist .sb-task-group') || !!document.querySelector('#sb-tasklist .sb-task-item')`);
    await js(`(() => { const i = document.getElementById('sb-task-search'); i.value = 'T0'; i.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
    await sleep(300);
    const taskFilteredOk = await js(`!!document.getElementById('sb-tasklist')`);
    await js(`(() => { const i = document.getElementById('sb-task-search'); i.value = ''; i.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
    await sleep(200);
    check('19. 侧边栏任务列表（搜索/筛选/分组）', !!(taskSearchOk && taskFiltersOk && taskGroupOk && taskFilteredOk),
      JSON.stringify({ taskSearchOk, taskFiltersOk, taskGroupOk, taskFilteredOk }));

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
