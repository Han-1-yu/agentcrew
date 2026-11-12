/**
 * 新建运行弹窗：目标 / 项目 / Mock / 自动模式 / 模型选择。
 */
import { BUILTIN_PROVIDERS, findProvider } from '../../contracts/providers.js';

import { apiFetch } from '../../lib/api.js';
import { $, $$, h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { readAlias, readNumber } from '../../lib/validate.js';
import { MODES, MODE_ORDER, currentMode, setMode } from '../../lib/mode.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { switchRun } from '../runs/index.js';
function openNewRunModal(opts = {}){
  // 记忆上次的模式选择；首次默认**真实模式**——Mock 产物是固定 Todo 夹具，与目标无关
  const saved = localStorage.getItem('agentcrew:nrMock');
  const mockDefault = saved == null ? !!(S.settings && S.settings.defaultMock) : saved === '1';
  // 执行模式（自动执行 / 计划审查 / 只读规划），读上次选择，默认 agent
  const modeDefault = currentMode();
  showModal(h('div',{},
    h('h3',{class:'modal-title'},'启动新运行'),
    h('div',{class:'nr-form'},
      // 最近使用的目标（异步填充；无历史时整行移除）
      h('div',{class:'nr-row',id:'nr-recent-row',style:'display:none'},
        h('label',{class:'nr-label',for:'nr-recent-goal'},'最近使用的目标'),
        h('select',{id:'nr-recent-goal',class:'nr-select'},
          h('option',{value:''},'选择历史目标…'))),

      // 目标输入 + 行内错误 + Demo 提示
      h('div',{class:'nr-row'},
        h('label',{class:'nr-label',for:'nr-goal'},'项目目标'),
        h('textarea',{id:'nr-goal',class:'nr-textarea',rows:3,
          placeholder:'例如：开发一个短链接生成服务，含 REST API、缓存与部署文档'}),
        h('div',{id:'nr-demo-hint',class:'nr-demo-hint',style:'display:none'},
          'Demo 模式：使用内置示例目标，无需手动输入'),
        h('div',{id:'nr-goal-error',class:'nr-field-error'})),

      // 模型选择
      h('div',{class:'nr-row',id:'nr-model-area'},
        h('label',{class:'nr-label'},'模型（可选，留空用设置默认）'),
        h('div',{class:'nr-model-row'},
          h('select',{id:'nr-provider',class:'nr-select fixed'},
            h('option',{value:''},'自动（设置默认）')),
          h('select',{id:'nr-model',class:'nr-select'},
            h('option',{value:''},'选择模型…')),
          h('input',{id:'nr-model-custom',class:'nr-input',type:'text',
            placeholder:'或手输 provider:model'})),
        h('div',{id:'nr-model-preview',class:'nr-model-preview'})),

      // 执行模式（对齐 WorkBuddy 的 Agent / Plan / Ask）+ 复选
      h('div',{class:'nr-row'},
        h('label',{class:'nr-label'},'执行模式'),
        h('div',{class:'nr-mode',id:'nr-mode'},
          MODE_ORDER.map(m => h('button',{
            type:'button',
            class:'nr-mode-btn' + (m === modeDefault ? ' active' : ''),
            'data-mode': m,
            onclick: (e) => {
              setMode(m);
              syncNrMode();
            },
          }, MODES[m].mode))),
        h('div',{id:'nr-mode-hint',class:'nr-hint'})),
      h('div',{class:'nr-checks'},
        h('label',{},h('input',{type:'checkbox',id:'nr-mock',checked:mockDefault}),'Mock 模式'),
        h('label',{},h('input',{type:'checkbox',id:'nr-demo'}),'Demo 目标')),

      // 高级选项（默认收起）
      h('div',{class:'nr-advanced',id:'nr-advanced'},
        h('button',{type:'button',class:'nr-advanced-toggle',id:'nr-advanced-toggle'},
          h('span',{class:'nr-advanced-arrow'},'▶'),
          h('span',{},'高级选项')),
        h('div',{class:'nr-advanced-body'},
          h('div',{class:'nr-row'},
            h('label',{class:'nr-label',for:'nr-project'},'项目（可选，用于分组管理）'),
            h('input',{id:'nr-project',class:'nr-input',type:'text',
              placeholder:'例如：shortlink-service'})),
          h('div',{class:'nr-adv-grid'},
            h('div',{class:'nr-row'},
              h('label',{class:'nr-label',for:'nr-maxcost'},'预算上限 (USD)'),
              h('input',{id:'nr-maxcost',class:'nr-input',type:'number',min:'0',step:'0.01',
                placeholder:'0 = 不限制'})),
            h('div',{class:'nr-row'},
              h('label',{class:'nr-label',for:'nr-max-llm-calls'},'max-llm-calls'),
              h('input',{id:'nr-max-llm-calls',class:'nr-input',type:'number',min:'0',step:'1',
                placeholder:'0 = 不限制'}))),
          h('label',{id:'nr-slow-wrap',class:'nr-slow-label',style:'visibility:hidden'},
            h('input',{type:'checkbox',id:'nr-slow'}),'慢速（便于观察/取消）'))),

      h('div',{id:'nr-hint',class:'nr-hint'})),
    h('div',{class:'modal-actions'},
      h('button',{class:'btn',onclick:closeModal},'取消'),
      h('button',{class:'btn btn-primary',id:'nr-start',onclick:async ()=>{
        const goalEl=$('#nr-goal');
        const goal=goalEl.value.trim();
        const useDemo=$('#nr-demo').checked;
        // 行内校验（不用 toast，避免轰炸）
        goalEl.classList.remove('input-error');
        const errBox=$('#nr-goal-error'); if(errBox) errBox.textContent='';
        if(!useDemo && !goal){
          goalEl.classList.add('input-error');
          if(errBox) errBox.textContent='请输入项目目标，或勾选 Demo 目标';
          goalEl.focus();
          return;
        }
        // 统一数值/别名校验：`type=number min=0` 挡不住 -5，parseFloat('1e999')
        // 是 Infinity，parseInt('1e999') 又会静默变成 1；自定义别名按后端
        // parse_alias 的 provider:model 口径提前拦住。
        const costR = readNumber($('#nr-maxcost'),
          { min: 0, max: 1000000, fallback: 0, label: '预算上限' });
        const callsR = readNumber($('#nr-max-llm-calls'),
          { min: 0, max: 10000000, integer: true, fallback: 0, label: 'max-llm-calls' });
        const aliasR = readAlias(assembleNrAlias());
        const paramError = costR.error || callsR.error || aliasR.error;
        if (paramError) { toast('error', '参数有误', paramError); return; }
        const project=$('#nr-project').value.trim();
        const mockOn=$('#nr-mock').checked;
        const startBtn=$('#nr-start');
        startBtn.disabled=true; startBtn.textContent='启动中…';
        try {
          localStorage.setItem('agentcrew:nrMock', mockOn ? '1' : '0');
          // C-6：快照必须在 startRun **之前**取——spawn 返回时 CLI 可能已经建好
          // run 目录，事后取快照会把新 run 算进 before，轮询 60s 恒找不到。
          let before = new Set();
          try {
            before = new Set(((await window.studio.listRuns())||[]).map(x=>x.dir));
          } catch { /* 列表读不到：退化为「不判重」，下面仍能找到 fresh */ }
          await window.studio.startRun({
            goal: useDemo ? '' : goal,
            demo: useDemo,
            mock: mockOn,
            mode: currentMode(),
            project: project || null,
            model: aliasR.value || null,
            maxCost: costR.value,
            maxLlmCalls: callsR.value,
            // 慢速 mock：拉长每次模拟 LLM 调用，留出观察与取消窗口
            env: (mockOn && $('#nr-slow').checked) ? { AGENTCREW_MOCK_LATENCY_MS: '2000' } : null,
          });
          closeModal();
          toast('success','已启动', mockOn ? 'Mock 运行已提交…' : '真实模型运行已提交，耗时数分钟…');
          // 轮询等 CLI 创建出**新的** run 目录再切换（不能取 runs[0]——可能是别的运行）
          let switched = false;
          for(let i=0;i<120;i++){
            await new Promise(res=>setTimeout(res,500));
            try {
              const fresh = ((await window.studio.listRuns())||[]).find(x=>!before.has(x.dir));
              if(fresh){ await switchRun(fresh.dir); switched = true; break; }
            } catch {}
          }
          // 60s 都没等到新目录时给一句明确反馈，别让用户对着静默的旧看板等。
          if(!switched) toast('info','已启动','未检测到新的 run 目录，请在运行列表中手动切换');
        } catch(e) {
          startBtn.disabled=false; startBtn.textContent='启动';
          toast('error','启动失败', String(e.message||e));
        }
      }},'启动'))));

  // 从底部命令输入框进来：预填目标，光标落到末尾（用户仍可在弹窗里改）
  if (opts && opts.goal) {
    const g = $('#nr-goal');
    if (g) {
      g.value = String(opts.goal);
      g.focus();
      try { g.setSelectionRange(g.value.length, g.value.length); } catch {}
    }
  }

  // 输入时自动清除目标错误态
  $('#nr-goal').addEventListener('input',()=>{
    $('#nr-goal').classList.remove('input-error');
    const eb=$('#nr-goal-error'); if(eb) eb.textContent='';
  });
  // 执行模式提示：随选择更新说明文案
  const MODE_HINTS = {
    agent: '全自动执行：规划 → 执行 → 三层门禁 → 交付，全程无人工卡点。',
    plan: '计划审查：规划完成后挂起，经「计划审查」页批准后才开始执行。',
    ask: '只读规划：只生成计划并落盘、不执行；之后可在运行详情点「恢复」续跑。',
  };
  const syncNrMode = () => {
    const m = currentMode();
    const hint = $('#nr-mode-hint');
    if (hint) hint.textContent = MODE_HINTS[m] || '';
    $$('.nr-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
  };
  syncNrMode();

  // Demo 勾选：目标框置灰 + 提示
  const syncDemo = () => {
    const on=$('#nr-demo').checked;
    $('#nr-goal').disabled = on;
    $('#nr-demo-hint').style.display = on ? '' : 'none';
    if(on){ $('#nr-goal').classList.remove('input-error'); const eb=$('#nr-goal-error'); if(eb) eb.textContent=''; }
  };
  $('#nr-demo').addEventListener('change',syncDemo);
  syncDemo();
  // 高级选项折叠
  $('#nr-advanced-toggle').addEventListener('click',()=>{
    $('#nr-advanced').classList.toggle('open');
  });
  // 最近使用的目标（F2 已持久化 recentGoals，最多 5 条）
  (async () => {
    const row = $('#nr-recent-row'); if(!row) return;
    let goals = [];
    try { goals = (await window.studio.recentGoals()) || []; } catch {}
    goals = Array.isArray(goals) ? goals.filter(g => g && String(g).trim()) : [];
    if(!goals.length){ row.remove(); return; }
    const sel = $('#nr-recent-goal');
    goals.slice(0,5).forEach(g => {
      const text = String(g);
      sel.appendChild(h('option',{value:text}, text.length>60 ? text.slice(0,60)+'…' : text));
    });
    row.style.display = '';
    sel.addEventListener('change',()=>{
      if(sel.value){
        $('#nr-goal').value = sel.value;
        $('#nr-goal').classList.remove('input-error');
        const eb=$('#nr-goal-error'); if(eb) eb.textContent='';
      }
    });
  })();

  // Mock 勾选联动：模式说明 + 慢速选项可见性 + 模型区置灰
  const syncMock = () => {
    const on=$('#nr-mock').checked;
    $('#nr-hint').textContent = on
      ? 'Mock 模式：不调用大模型，产物为内置 Todo 示例（与目标无关），适合演示界面与调试。'
      : '真实模式：调用 .env 配置的大模型（如 deepseek-chat）按目标生成真实产物，通常花费 $0.01–0.2、需数分钟；需先在 .env 配置 API Key。';
    $('#nr-slow-wrap').style.visibility = on ? 'visible' : 'hidden';
    // Mock 勾选优先：模型选择区置灰（--mock 时别名不参与 CLI 参数）
    ['#nr-provider', '#nr-model', '#nr-model-custom'].forEach(sel => {
      const el = $(sel); if (el) el.disabled = on;
    });
    const area = $('#nr-model-area');
    if (area) area.style.opacity = on ? '0.45' : '1';
  };
  $('#nr-mock').addEventListener('change',syncMock);
  syncMock();

  // B2：模型选择 —— provider 下拉 → 模型下拉 → 组装 provider:model 别名；后端不可用时可手输
  const nrProvider = $('#nr-provider');
  const nrModel = $('#nr-model');
  const nrModelCustom = $('#nr-model-custom');
  const nrPreview = $('#nr-model-preview');
  BUILTIN_PROVIDERS.forEach(p => nrProvider.appendChild(h('option', { value: p.key }, p.label)));

  const updateNrPreview = () => {
    const c = (nrModelCustom.value || '').trim();
    nrPreview.textContent = c ? `将使用：${c}`
      : (nrProvider.value && nrModel.value ? `将使用：${nrProvider.value}:${nrModel.value}` : '');
  };
  const assembleNrAlias = () => {
    const c = (nrModelCustom.value || '').trim();
    if (c) return c;
    return (nrProvider.value && nrModel.value) ? `${nrProvider.value}:${nrModel.value}` : '';
  };
  const refreshNrModels = async (providerKey) => {
    nrModel.innerHTML = '<option value="">选择模型…</option>';
    updateNrPreview();
    if (!providerKey) return;
    // 先放内置 suggested_models，保证立即可选
    const builtin = findProvider(providerKey);
    ((builtin && builtin.suggested_models) || []).forEach(m => nrModel.appendChild(h('option', { value: m }, m)));
    // 异步拉后端模型列表，成功则替换（失败保留内置）
    try {
      const data = await apiFetch(`/providers/${encodeURIComponent(providerKey)}/models`);
      const models = (data && data.models) || [];
      if (models.length) {
        nrModel.innerHTML = '<option value="">选择模型…</option>';
        models.forEach(m => {
          const id = typeof m === 'string' ? m : (m.id || m.name || '');
          if (id) nrModel.appendChild(h('option', { value: id }, id));
        });
      }
    } catch (e) { console.log('[apiFetch] models fallback:', e.message); }
  };
  nrProvider.addEventListener('change', () => { nrModelCustom.value = ''; refreshNrModels(nrProvider.value); });
  nrModel.addEventListener('change', updateNrPreview);
  nrModelCustom.addEventListener('input', updateNrPreview);
  updateNrPreview();
  // 异步拉后端 provider 列表（失败保留内置）
  (async () => {
    try {
      const data = await apiFetch('/providers');
      const list = (data && data.providers) || [];
      if (list.length) {
        nrProvider.innerHTML = '<option value="">自动（设置默认）</option>';
        list.forEach(p => nrProvider.appendChild(h('option', { value: p.name }, p.name)));
      }
    } catch (e) { console.log('[apiFetch] providers fallback:', e.message); }
  })();
}

export { openNewRunModal };
