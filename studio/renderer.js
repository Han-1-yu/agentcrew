'use strict';
// AgentCrew Studio — 渲染进程（原生 JS，无框架）。
// 第一阶段：iOS 风格运行总览 Dashboard。
// 数据层严格对齐 artifacts/contracts：TaskStatus 10 态、ledger 事件 {ts,run_id,event,data}。
// 数据层（S.dash）与视图层分离；无真实运行时用内置 mock。

/* ============================================================
   常量：状态 / 角色 / 事件元数据（对齐 contracts/state-enums.md）
   ============================================================ */

// TaskStatus 10 态（权威：task/state_machine.py）
const STATUS_META = {
  pending:   { label: '待启动', color: '#8E8E93', fill: '#FFFFFF' },
  ready:     { label: '就绪',   color: '#007AFF', fill: 'rgba(0,122,255,0.06)' },
  running:   { label: '运行中', color: '#007AFF', fill: 'rgba(0,122,255,0.10)' },
  review:    { label: '门禁评审', color: '#FF9F0A', fill: 'rgba(255,159,10,0.10)' },
  rework:    { label: '返工中', color: '#FF9F0A', fill: 'rgba(255,159,10,0.14)' },
  blocked:   { label: '阻塞',   color: '#FF9F0A', fill: 'rgba(255,159,10,0.08)' },
  succeeded: { label: '已通过', color: '#34C759', fill: 'rgba(52,199,89,0.10)' },
  failed:    { label: '失败',   color: '#FF3B30', fill: 'rgba(255,59,48,0.10)' },
  skipped:   { label: '已跳过', color: '#8E8E93', fill: '#F2F2F7' },
  cancelled: { label: '已取消', color: '#8E8E93', fill: '#F2F2F7' },
  unknown:   { label: '未知',   color: '#8E8E93', fill: '#FFFFFF' },
};
const STATUS_ORDER = ['pending','ready','running','review','rework','blocked','succeeded','failed','skipped','cancelled'];

// RunStatus（实际观测值）：preparing/planning/awaiting_approval/running/paused/failed/completed/cancelled
const RUN_META = {
  preparing:        { label: '准备中',       dot: 'idle',    chip: 'idle' },
  planning:         { label: '规划中',       dot: 'running', chip: 'review' },
  awaiting_approval:{ label: '等待人工确认', dot: 'warning', chip: 'review' },
  running:          { label: '运行中',       dot: 'running', chip: 'running' },
  paused:           { label: '已暂停',       dot: 'paused',  chip: 'paused' },
  failed:           { label: '失败',         dot: 'error',   chip: 'failed' },
  completed:        { label: '已完成',       dot: 'done',    chip: 'done' },
  cancelled:        { label: '已取消',       dot: 'idle',    chip: 'idle' },
};

// RunPhase：planning / execution / delivery（代码路径，UI 派生）
const PHASES = ['规划', '执行', '交付'];

// 8 个角色（role 字段权威值）
const ROLE_META = {
  project_manager:  { label: 'PM',       full: '项目经理',   color: '#AF52DE' },
  product_manager:  { label: '产品',     full: '产品经理',   color: '#FF2D55' },
  architect:        { label: '架构',     full: '架构师',     color: '#007AFF' },
  backend_dev:      { label: '后端',     full: '后端开发',   color: '#34C759' },
  frontend_dev:     { label: '前端',     full: '前端开发',   color: '#5AC8FA' },
  qa_engineer:      { label: '测试',     full: '测试工程师', color: '#FF9500' },
  devops_engineer:  { label: '运维',     full: '运维工程师', color: '#5E5CE6' },
  technical_writer: { label: '文档',     full: '文档工程师', color: '#8E8E93' },
};
const roleMeta = (id) => ROLE_META[id] || { label: id || '?', full: id || '未知角色', color: '#8E8E93' };

// ledger 事件类型（event 字段权威值，见 DATA-CONTRACT-REFERENCE §3）
const EVENT_META = {
  run_started:      { label: '运行创建',   color: '#007AFF' },
  llm_call:         { label: 'LLM 调用',   color: '#5AC8FA' },
  plan_created:     { label: '任务生成',   color: '#34C759' },
  plan_scored:      { label: '计划评分',   color: '#5AC8FA' },
  task_started:     { label: '任务开始',   color: '#FF9500' },
  task_notes:       { label: '任务笔记',   color: '#8E8E93' },
  artifact_written: { label: '产物生成',   color: '#34C759' },
  task_finished:    { label: '任务完成',   color: '#34C759' },
  task_transition:  { label: '状态跃迁',   color: '#8E8E93' },
  gate_result:      { label: '门禁结果',   color: '#AF52DE' },
  run_finished:     { label: '运行结束',   color: '#8E8E93' },
  llm_failover:     { label: '故障转移',   color: '#FF9F0A' },
  llm_model_switched:{ label: '模型切换',  color: '#5AC8FA' },
  budget_warning:   { label: '预算预警',   color: '#FF9F0A' },
  budget_exhausted: { label: '预算耗尽',   color: '#FF3B30' },
  task_timeout:     { label: '任务超时',   color: '#FF9F0A' },
  checkpoint_saved: { label: '检查点保存', color: '#8E8E93' },
  human_approved:   { label: '人工批准',   color: '#34C759' },
  run_cancelled:    { label: '运行取消',   color: '#FF3B30' },
  error:            { label: '错误',       color: '#FF3B30' },
};
const EVENT_TYPES = Object.keys(EVENT_META);

// iOS 硬编码颜色
const C = { blue:'#007AFF', success:'#34C759', warn:'#FF9F0A', error:'#FF3B30',
  text:'#1C1C1E', sub:'#6E6E73', ter:'#8E8E93' };

/* ============================================================
   全局状态
   ============================================================ */
const S = {
  settings: null, env: null,
  dash: null,            // { run, tasks, agents, gates, events }
  nav: 'dashboard',
  detail: null,          // { kind:'task'|'agent', id }
  eventFilter: new Set(EVENT_TYPES),
  eventExpanded: new Set(),
  simTimer: null,
  startedAt: Date.now(),
  view: { zoom: 1, x: 0, y: 0 },   // DAG 视图变换
};

// 自测遥测（selftest 依赖，必须保留）
window.__st = {
  snapshots: 0, ledgerBatches: 0,
  state: () => {
    const d = S.dash || {};
    return {
      dir: (d.run && d.run.dir) || null,
      status: (d.run && d.run.status) || 'idle',
      stats: (d.run && d.run.stats) || null,
      tasks: (d.tasks || []).length,
      events: (d.events || []).length,
    };
  },
};

/* ============================================================
   工具函数
   ============================================================ */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => [...(root || document).querySelectorAll(sel)];

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') el.setAttribute('style', v);
    else if (k.startsWith('data-')) el.setAttribute(k, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el[k] = v;
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return el;
}
const SVG_NS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const c of children.flat()) {
    if (c == null) continue;
    el.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return el;
}

function fmtTokens(n) { return n == null ? '—' : Number(n).toLocaleString('en-US'); }
function fmtCost(n) { if (n==null) return '—'; const v=Number(n); return `$${v.toFixed(4).replace(/0+$/,'').replace(/\.$/,'')}`; }
function fmtDur(ms) {
  if (ms==null) return '—';
  if (ms<1000) return `${Math.round(ms)}ms`;
  const sec=ms/1000; if (sec<60) return `${sec.toFixed(1)}s`;
  const m=Math.floor(sec/60); if (m<60) return `${m}m${Math.round(sec%60)}s`;
  return `${Math.floor(m/60)}h${m%60}m`;
}
function fmtBytes(n) { if(n==null)return'—'; if(n<1024)return`${n} B`; if(n<1048576)return`${(n/1024).toFixed(1)} KB`; return `${(n/1048576).toFixed(2)} MB`; }
// ledger ts: "YYYY-MM-DD HH:MM:SS.ffffff"（空格分隔，无时区）
function parseTs(ts) { if(!ts) return null; const d=new Date(String(ts).replace(' ','T')); return isNaN(d)?null:d; }
function fmtClock(ts){ const d=parseTs(ts); return d?d.toTimeString().slice(0,8):String(ts||''); }
function fmtDateTime(ts){ const d=parseTs(ts); if(!d)return'—'; const p=x=>String(x).padStart(2,'0'); return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; }
function relTime(ts){ const d=parseTs(ts); if(!d)return''; const diff=(Date.now()-d.getTime())/1000; if(diff<5)return'刚刚'; if(diff<60)return`${Math.floor(diff)} 秒前`; if(diff<3600)return`${Math.floor(diff/60)} 分钟前`; if(diff<86400)return`${Math.floor(diff/3600)} 小时前`; return `${Math.floor(diff/86400)} 天前`; }
function trunc(s,n){ s=String(s==null?'':s); return s.length>n?s.slice(0,n)+'…':s; }

async function callIpc(fn, ...args) {
  try { return await fn(...args); }
  catch (e) { toast('error','后端调用失败', String(e&&e.message||e)); throw e; }
}

/* ============================================================
   Toast / Modal / 确认弹窗
   ============================================================ */
function toast(type, title, msg, ms) {
  const box = $('#toasts');
  const t = h('div',{class:`toast ${type||''}`}, h('div',{class:'toast-title'},title), msg?h('div',{class:'toast-msg'},msg):null);
  box.appendChild(t);
  setTimeout(()=>{ t.style.opacity='0'; t.style.transition='opacity .25s'; setTimeout(()=>t.remove(),300); }, ms||(type==='error'?8000:4000));
}
function showModal(node){ const m=$('#modal'); m.innerHTML=''; m.appendChild(node); $('#modal-mask').classList.remove('hidden'); m.classList.remove('hidden'); requestAnimationFrame(()=>{ m.classList.add('show'); $('#modal-mask').classList.add('show'); }); }
function closeModal(){ const m=$('#modal'); m.classList.remove('show'); $('#modal-mask').classList.remove('show'); setTimeout(()=>{ m.classList.add('hidden'); $('#modal-mask').classList.add('hidden'); },200); }
$('#modal-mask').addEventListener('click', closeModal);
function confirmAction(title, message, onOk, okLabel) {
  showModal(h('div',{},
    h('h3',{class:'modal-title'},title),
    h('div',{class:'confirm-text'},message),
    h('div',{class:'modal-actions'},
      h('button',{class:'btn',onclick:closeModal},'取消'),
      h('button',{class:'btn btn-danger',onclick:()=>{closeModal();onOk();}},okLabel||'确认'))));
}

/* ============================================================
   Mock 数据（对齐 reference-run 真实结构）
   ============================================================ */
// 生成 ledger 风格 ts："YYYY-MM-DD HH:MM:SS.ffffff"
function nowTs(offsetMin) {
  const d = new Date(Date.now() - (offsetMin||0)*60000);
  const p = (x,n=2)=>String(x).padStart(n,'0');
  const us = String(d.getMilliseconds()).padStart(3,'0')+'000';
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${us}`;
}
const RUN_ID = 'run-a7f3c2';

function buildMockRun() {
  return {
    run_id: RUN_ID,
    dir: null,
    project_goal: '开发一个高并发短链接生成服务：含 REST API、Redis 缓存、限流、部署流水线与完整文档',
    status: 'running',          // RunStatus
    phase: 'execution',         // planning/execution/delivery
    provider: 'OpenAI',
    model: 'gpt-4o',
    budget_limit_usd: 1.0,
    started_at: nowTs(12),
    updated_at: nowTs(0),
    config_snapshot: { default_model: 'gpt-4o', max_concurrency: 4, mock: false, max_cost_usd: 1.0 },
    stats: {
      started_at: nowTs(12), finished_at: null,
      total_tasks: 14, succeeded: 4, failed: 0, blocked: 1, skipped: 0,
      llm_calls: 87, prompt_tokens: 142300, completion_tokens: 38400, estimated_cost_usd: 0.3821,
    },
  };
}

// 任务：字段对齐 tasks.json。dependencies / attempts / rework_count / result(null) / reviews
function T(id, title, role, deps, status, extra) {
  return Object.assign({
    id, title, description: '', brief: '', role, priority: 5,
    dependencies: deps, attempts: 1, rework_count: 0,
    max_attempts: 3, max_reworks: 2, timeout_seconds: 300,
    status, result: null, reviews: [],
    progress: status==='running' ? 45 : 0,
    error: null,
  }, extra||{});
}
function buildMockTasks() {
  return [
    T('T-01','需求收集与澄清','project_manager',[],'succeeded',{brief:'与干系人确认功能边界与 SLA', result:{summary:'已澄清并发目标 5000 QPS 与 99.9% SLA', duration_ms:420000, artifacts:[{path:'docs/PRD.md',size_bytes:8200}]}}),
    T('T-02','产品原型定义','product_manager',[],'succeeded',{brief:'输出 API 草图与验收标准', result:{summary:'完成 API 草图与页面流程', duration_ms:510000, artifacts:[{path:'docs/prototype.md',size_bytes:5400}]}}),
    T('T-03','系统架构设计','architect',['T-01'],'succeeded',{brief:'分层架构与技术选型', result:{summary:'确定分层架构与 Redis 缓存策略', duration_ms:680000, artifacts:[{path:'docs/arch.md',size_bytes:12000}]}}),
    T('T-04','前端原型搭建','frontend_dev',['T-02'],'succeeded',{brief:'后台页面与落地页原型', result:{summary:'完成控制台与跳转落地页原型', duration_ms:590000, artifacts:[{path:'frontend/proto.html',size_bytes:6100}]}}),
    T('T-05','数据模型设计','backend_dev',['T-03'],'running',{brief:'设计 url_map 表与 TTL 策略', progress:62}),
    T('T-06','API 接口设计','architect',['T-03','T-05'],'blocked',{brief:'定义 RESTful 接口与错误码', description:'定义 RESTful 接口、错误码与鉴权方案。'}),
    T('T-07','后端用户模块','backend_dev',['T-05','T-06'],'pending',{brief:'注册、API Key 与鉴权中间件'}),
    T('T-08','后端短链模块','backend_dev',['T-05','T-06'],'pending',{brief:'短链生成、映射解析与统计'}),
    T('T-09','前端页面实现','frontend_dev',['T-06','T-04'],'pending',{brief:'对接接口实现控制台与落地页'}),
    T('T-10','测试用例编写','qa_engineer',['T-07','T-08'],'pending',{brief:'接口与端到端测试用例'}),
    T('T-11','集成联调','backend_dev',['T-07','T-08','T-09'],'pending',{brief:'前后端打通完整链路'}),
    T('T-12','部署流水线','devops_engineer',['T-11'],'pending',{brief:'CI/CD、容器化与灰度发布'}),
    T('T-13','系统测试','qa_engineer',['T-11','T-10'],'pending',{brief:'压测、回归与安全扫描'}),
    T('T-14','交付文档','technical_writer',['T-13','T-12'],'pending',{brief:'部署、API 与运维文档'}),
  ];
}

function buildMockAgents() {
  // agent status 派生自任务：queued/executing/reviewing/done
  return {
    project_manager:  { status:'done',      task:'空闲',     action:'已交付需求文档',   done:1, fail:0, model:'gpt-4o',      tokens:8200,  cost:0.021, updated:nowTs(10) },
    product_manager:  { status:'done',      task:'空闲',     action:'已完成原型评审',   done:1, fail:0, model:'gpt-4o',      tokens:11400, cost:0.034, updated:nowTs(9) },
    architect:        { status:'queued',    task:'T-06 API 接口设计', action:'等待 T-05 数据模型', done:1, fail:0, model:'gpt-4o', tokens:15600, cost:0.048, updated:nowTs(1) },
    backend_dev:      { status:'executing', task:'T-05 数据模型设计', action:'正在建表与索引',   done:0, fail:0, model:'gpt-4o', tokens:9800, cost:0.029, updated:nowTs(0) },
    frontend_dev:     { status:'done',      task:'空闲',     action:'等待接口契约',     done:1, fail:0, model:'gpt-4o', tokens:9800, cost:0.027, updated:nowTs(8) },
    qa_engineer:      { status:'queued',    task:'空闲',     action:'等待可测构建',     done:0, fail:0, model:'gpt-4o-mini', tokens:0, cost:0, updated:nowTs(12) },
    devops_engineer:  { status:'queued',    task:'空闲',     action:'等待联调产物',     done:0, fail:0, model:'gpt-4o-mini', tokens:0, cost:0, updated:nowTs(12) },
    technical_writer: { status:'queued',    task:'空闲',     action:'等待交付物',       done:0, fail:0, model:'gpt-4o-mini', tokens:0, cost:0, updated:nowTs(12) },
  };
}

// 三层门禁：layer static(G1)/llm(G2)/delivery(G3)。状态为 UI 派生聚合。
function buildMockGates() {
  return [
    { id:'G1', layer:'static', name:'静态校验', status:'running', checks:{total:12, passed:8, failed:0}, duration_ms:null,
      verdict:'正在运行 lint / 类型检查 / 依赖扫描', rework:true, need_human:false, task_ref:'T-07, T-08' },
    { id:'G2', layer:'llm', name:'LLM 评审', status:'pending', checks:{total:8, passed:0, failed:0}, duration_ms:null,
      verdict:'等待集成联调完成后触发', rework:true, need_human:true, task_ref:'T-11' },
    { id:'G3', layer:'delivery', name:'交付确认', status:'pending', checks:{total:5, passed:0, failed:0}, duration_ms:null,
      verdict:'等待系统测试通过后由人工确认', rework:false, need_human:true, task_ref:'T-14' },
  ];
}

// ledger 事件：{ ts, run_id, event, data }
function ev(tsOffset, event, data, taskId, role) {
  return { ts: nowTs(tsOffset), run_id: RUN_ID, event, data: Object.assign(taskId?{task_id:taskId}:{}, role?{role}:{}, data||{}) };
}
function buildMockEvents() {
  return [
    ev(12,'run_started',{ goal:'短链接生成服务' }),
    ev(11,'plan_created',{ task_count:14, roles:Object.keys(ROLE_META), planner_attempts:1 }),
    ev(11,'plan_scored',{ total:0.81, coverage:0.8, specificity:0.85, dependency_quality:0.9, deliverable_clarity:0.8 }),
    ev(10,'task_started',{ attempt:1 },'T-01','project_manager'),
    ev(10,'task_finished',{ attempt:1, duration_ms:420000 },'T-01','project_manager'),
    ev(9,'task_started',{ attempt:1 },'T-02','product_manager'),
    ev(9,'task_finished',{ attempt:1, duration_ms:510000 },'T-02','product_manager'),
    ev(7,'task_started',{ attempt:1 },'T-03','architect'),
    ev(7,'task_finished',{ attempt:1, duration_ms:680000 },'T-03','architect'),
    ev(6,'task_started',{ attempt:1 },'T-04','frontend_dev'),
    ev(6,'task_finished',{ attempt:1, duration_ms:590000 },'T-04','frontend_dev'),
    ev(3,'checkpoint_saved',{ reason:'周期检查点' }),
    ev(2,'task_started',{ attempt:1 },'T-05','backend_dev'),
    ev(1,'llm_failover',{ from:'OpenAI', to:'Anthropic', reason:'429 rate limit' }),
    ev(0,'budget_warning',{ pct:38 }),
  ];
}

function initMockData() {
  S.dash = {
    run: buildMockRun(), tasks: buildMockTasks(),
    agents: buildMockAgents(), gates: buildMockGates(), events: buildMockEvents(),
  };
}

/* ============================================================
   DAG 布局（拓扑分层，用 dependencies 字段）
   ============================================================ */
function computeLayers(tasks) {
  const byId = new Map(tasks.map(t=>[t.id,t]));
  const layerOf = new Map(tasks.map(t=>[t.id,0]));
  let changed=true, guard=0;
  while (changed && guard++<50) {
    changed=false;
    for (const t of tasks) {
      const deps=t.dependencies||[]; if(!deps.length) continue;
      const up=Math.max(...deps.map(d=>layerOf.get(d)??0));
      if (layerOf.get(t.id)!==up+1){ layerOf.set(t.id,up+1); changed=true; }
    }
  }
  const layers=[];
  for (const [id,l] of layerOf){ while(layers.length<=l) layers.push([]); layers[l].push(id); }
  return { byId, layerOf, layers };
}
const NW=208, NH=86, COL_GAP=72, ROW_GAP=48;
function layoutDAG(tasks) {
  const { byId, layerOf, layers } = computeLayers(tasks);
  // 重心启发式排序：减少层间连线交叉
  for (let pass = 0; pass < 3; pass++) {
    // 前向：根据上游位置排序
    for (let li = 1; li < layers.length; li++) {
      layers[li].sort((a, b) => {
        const aDeps = (byId.get(a).dependencies || []).map(d => layers[li-1].indexOf(d)).filter(i => i >= 0);
        const bDeps = (byId.get(b).dependencies || []).map(d => layers[li-1].indexOf(d)).filter(i => i >= 0);
        const aCenter = aDeps.length ? aDeps.reduce((s,v)=>s+v,0)/aDeps.length : layers[li-1].length/2;
        const bCenter = bDeps.length ? bDeps.reduce((s,v)=>s+v,0)/bDeps.length : layers[li-1].length/2;
        return aCenter - bCenter || a.localeCompare(b);
      });
    }
    // 后向：根据下游位置排序
    for (let li = layers.length - 2; li >= 0; li--) {
      layers[li].sort((a, b) => {
        const aDown = layers[li+1].filter(id => (byId.get(id).dependencies || []).includes(a)).map(id => layers[li+1].indexOf(id));
        const bDown = layers[li+1].filter(id => (byId.get(id).dependencies || []).includes(b)).map(id => layers[li+1].indexOf(id));
        const aCenter = aDown.length ? aDown.reduce((s,v)=>s+v,0)/aDown.length : layers[li+1].length/2;
        const bCenter = bDown.length ? bDown.reduce((s,v)=>s+v,0)/bDown.length : layers[li+1].length/2;
        return aCenter - bCenter || a.localeCompare(b);
      });
    }
  }
  const pos = new Map();
  layers.forEach((ids,li)=>{ ids.forEach((id,ri)=>pos.set(id,{x:li*(NW+COL_GAP), y:ri*(NH+ROW_GAP)})); });
  const maxLayer=layers.length-1, maxRows=Math.max(...layers.map(l=>l.length));
  return { byId, layers, pos, width:(maxLayer+1)*(NW+COL_GAP), height:maxRows*(NH+ROW_GAP) };
}

/* ============================================================
   SVG DAG 渲染
   ============================================================ */
function renderDAG() {
  const svg=$('#dag-svg'); if(!svg) return;
  const tasks=S.dash.tasks; const L=layoutDAG(tasks);
  svg.innerHTML='';
  const pad=32, totalW=L.width+pad*2, totalH=L.height+pad*2;
  svg.setAttribute('viewBox',`0 0 ${totalW} ${totalH}`);

  // SVG defs: 箭头 marker
  const defs=s('defs',{});
  defs.appendChild(s('marker',{id:'arrow-hot',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto'},
    s('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#007AFF'})));
  defs.appendChild(s('marker',{id:'arrow-done',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto'},
    s('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#34C759'})));
  defs.appendChild(s('marker',{id:'arrow-pending',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto'},
    s('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'rgba(60,60,67,0.3)'})));
  svg.appendChild(defs);

  const vp=s('g',{id:'dag-viewport', transform:`translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})`});

  const runningIds=new Set(tasks.filter(t=>['running','review','rework'].includes(t.status)).map(t=>t.id));
  const hotEdges=new Set();
  for (const rid of runningIds){ const q=[rid],seen=new Set();
    while(q.length){ const cur=q.shift(); if(seen.has(cur))continue; seen.add(cur);
      const t=L.byId.get(cur); if(!t)continue;
      for(const dep of (t.dependencies||[])){ hotEdges.add(dep+'->'+cur); q.push(dep); } } }

  // 连线：正交折线（右侧→左侧），箭头 + 状态色
  for (const t of tasks){
    const to=L.pos.get(t.id); if(!to) continue;
    for(const dep of (t.dependencies||[])){
      const from=L.pos.get(dep); if(!from)continue;
      // 从源节点右侧中心 → 目标节点左侧中心
      const x1=from.x+NW, y1=from.y+NH/2, x2=to.x, y2=to.y+NH/2;
      const midX=(x1+x2)/2;
      const hot=hotEdges.has(dep+'->'+t.id);
      const up=L.byId.get(dep);
      const upDone=up&&up.status==='succeeded';
      const upFailed=up&&up.status==='failed';
      let stroke, sw, marker, dash='none';
      if(hot){ stroke='#007AFF'; sw=2.2; marker='url(#arrow-hot)'; }
      else if(upDone){ stroke='#34C759'; sw=1.6; marker='url(#arrow-done)'; }
      else if(upFailed){ stroke='#FF9F0A'; sw=1.4; marker='url(#arrow-pending)'; dash='4 3'; }
      else { stroke='rgba(60,60,67,0.25)'; sw=1.2; marker='url(#arrow-pending)'; }
      // 正交折线：右出 → 水平 → 垂直 → 水平 → 左入
      vp.appendChild(s('path',{ d:`M ${x1} ${y1} H ${midX} V ${y2} H ${x2-2}`,
        fill:'none', stroke, 'stroke-width':sw, 'stroke-dasharray':dash, 'marker-end':marker, opacity: hot?1:0.8 }));
    }
  }

  // 节点
  for (const t of tasks){
    const p=L.pos.get(t.id); if(!p)continue;
    const meta=STATUS_META[t.status]||STATUS_META.unknown;
    const rm=roleMeta(t.role);
    const isRetry=(t.attempts||1)>1 && ['running','ready','rework'].includes(t.status);
    const isRunning=t.status==='running';
    const isDone=t.status==='succeeded';
    const isFailed=t.status==='failed';

    const node=s('g',{class:'dag-node'+(isRunning?' running':''), transform:`translate(${p.x+pad} ${p.y+pad})`, 'data-task':t.id});

    // 背景
    node.appendChild(s('rect',{class:'node-box', width:NW, height:NH, rx:6,
      fill:meta.fill, stroke:meta.color,
      'stroke-width':isRunning?2.4:(isDone?1.8:1.3),
      'stroke-dasharray':isRetry?'5 3':'none' }));
    // 左侧状态色条
    node.appendChild(s('rect',{x:0,y:0,width:5,height:NH,rx:2.5,fill:meta.color}));

    // 顶部：task_id + 耗时
    node.appendChild(s('text',{x:14,y:18,'font-size':10,fill:'#8E8E93','font-family':'ui-monospace, Menlo, monospace'}, t.id));
    if (t.result && t.result.duration_ms!=null)
      node.appendChild(s('text',{x:NW-12,y:18,'font-size':9,fill:'#8E8E93','text-anchor':'end','font-family':'ui-monospace, Menlo, monospace'}, fmtDur(t.result.duration_ms)));

    // 标题
    node.appendChild(s('text',{x:14,y:40,'font-size':13,'font-weight':600,fill:'#1C1C1E'}, trunc(t.title,18)));

    // 底部：角色头像 + 名称 + 状态
    const avatarY=62;
    // 角色圆形头像
    node.appendChild(s('circle',{cx:21,cy:avatarY,r:9,fill:rm.color,opacity:0.9}));
    node.appendChild(s('text',{x:21,y:avatarY+3.5,'font-size':9,'font-weight':700,fill:'#fff','text-anchor':'middle'}, rm.label.charAt(0)));
    // 角色名
    node.appendChild(s('text',{x:34,y:avatarY+3,'font-size':10,fill:'#6E6E73'}, rm.full));
    // 状态标签（右侧，实心小 chip）
    const chipW = isRetry?48:42;
    node.appendChild(s('rect',{x:NW-chipW-10,y:avatarY-8,width:chipW,height:16,rx:3,fill:meta.color}));
    node.appendChild(s('text',{x:NW-chipW/2-10,y:avatarY+3,'font-size':9,'font-weight':600,fill:'#fff','text-anchor':'middle'}, isRetry?'重试中':meta.label));

    // 完成对勾（右上角）
    if(isDone){
      node.appendChild(s('circle',{cx:NW-16,cy:16,r:8,fill:'#34C759'}));
      node.appendChild(s('path',{d:'M 11 16 L 14.5 19.5 L 20 13',stroke:'#fff','stroke-width':2,fill:'none','stroke-linecap':'round','stroke-linejoin':'round',transform:`translate(${NW-28},0)`}));
    }
    // 返工次数
    if ((t.rework_count||0)>0)
      node.appendChild(s('text',{x:NW-14,y:40,'font-size':9,fill:'#FF9F0A','text-anchor':'end','font-weight':600},`↻${t.rework_count}`));

    // running：底部进度条
    if (isRunning){
      node.appendChild(s('rect',{x:14,y:NH-10,width:NW-28,height:3,rx:1.5,fill:'rgba(60,60,67,0.1)'}));
      node.appendChild(s('rect',{x:14,y:NH-10,width:(NW-28)*(t.progress||0)/100,height:3,rx:1.5,fill:'#007AFF'}));
    }
    // failed：错误摘要
    if (isFailed && t.error)
      node.appendChild(s('text',{x:14,y:NH-12,'font-size':9,fill:'#FF3B30'}, trunc(t.error,22)));

    node.addEventListener('click',()=>openTaskDetail(t.id));
    vp.appendChild(node);
  }
  svg.appendChild(vp);
  wireDAGControls(svg, vp, totalW, totalH);
}

function wireDAGControls(svg, vp, totalW, totalH) {
  const wrap=$('#dag-canvas-wrap');
  const apply=()=>{ vp.setAttribute('transform',`translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})`);
    const lbl=$('#dag-zoom-label'); if(lbl) lbl.textContent=Math.round(S.view.zoom*100)+'%';
    try { localStorage.setItem('agentcrew:dagView', JSON.stringify(S.view)); } catch {} };
  const zoomAt=(factor,cx,cy)=>{ const nz=Math.max(0.3,Math.min(3,S.view.zoom*factor));
    const rect=wrap.getBoundingClientRect(); const px=cx??rect.width/2, py=cy??rect.height/2;
    const wx=(px-S.view.x)/S.view.zoom, wy=(py-S.view.y)/S.view.zoom;
    S.view.zoom=nz; S.view.x=px-wx*nz; S.view.y=py-wy*nz; apply(); };
  $('#dag-zoom-in').onclick=()=>zoomAt(1.2);
  $('#dag-zoom-out').onclick=()=>zoomAt(1/1.2);
  $('#dag-reset').onclick=()=>{ S.view.zoom=1;S.view.x=0;S.view.y=0;apply(); };
  $('#dag-fit').onclick=()=>{ const rect=wrap.getBoundingClientRect();
    S.view.zoom=Math.min(1.2,Math.max(0.3,Math.min(rect.width/totalW,rect.height/totalH)));
    S.view.x=(rect.width-totalW*S.view.zoom)/2; S.view.y=(rect.height-totalH*S.view.zoom)/2; apply(); };
  wrap.onwheel=(e)=>{ e.preventDefault(); const r=wrap.getBoundingClientRect();
    zoomAt(e.deltaY<0?1.1:1/1.1, e.clientX-r.left, e.clientY-r.top); };
  let dragging=false,sx=0,sy=0,ox=0,oy=0;
  wrap.onmousedown=(e)=>{ if(e.target.closest('.dag-node'))return;
    dragging=true; wrap.classList.add('panning'); sx=e.clientX;sy=e.clientY;ox=S.view.x;oy=S.view.y; };
  window.addEventListener('mousemove',(e)=>{ if(!dragging)return; S.view.x=ox+(e.clientX-sx); S.view.y=oy+(e.clientY-sy); apply(); });
  window.addEventListener('mouseup',()=>{ dragging=false; wrap.classList.remove('panning'); });
}

function renderDAGFallback() {
  const host=$('#dag-list-fallback'); if(!host)return; host.innerHTML='';
  const tasks=S.dash.tasks; const groups={};
  for(const t of tasks){ (groups[t.status]=groups[t.status]||[]).push(t); }
  const order=['running','review','rework','ready','blocked','pending','succeeded','failed','skipped'];
  for(const st of order){ const items=groups[st]; if(!items||!items.length)continue;
    const meta=STATUS_META[st];
    host.appendChild(h('div',{class:'dag-group-title'},`${meta.label}（${items.length}）`));
    for(const t of items){ const rm=roleMeta(t.role);
      host.appendChild(h('div',{class:'dag-task-row',onclick:()=>openTaskDetail(t.id)},
        h('span',{class:'dot',style:`background:${meta.color}`}),
        h('span',{class:'mono',style:`color:${C.ter}`},t.id),
        h('span',{},t.title),
        h('span',{style:`color:${rm.color};margin-left:auto;font-size:11px`},rm.full)));
    }
  }
}

/* ============================================================
   Dashboard 渲染
   ============================================================ */
function renderRunStatusBar() {
  const host=$('#run-status-bar'); if(!host)return;
  const run=S.dash.run; const meta=RUN_META[run.status]||RUN_META.idle;
  host.className = ''; host.classList.add('status-'+(run.status||'idle'));
  const stats=run.stats||{};
  const elapsed=run.status==='running'&&stats.started_at ? (Date.now()-parseTs(stats.started_at).getTime()) : null;
  const cost=stats.estimated_cost_usd||0;
  const actions=[];
  const isReal = !!S.realMode;
  if (run.status==='running'){
    if (!isReal) {
      actions.push(h('button',{class:'btn',onclick:()=>confirmAction('暂停运行','暂停后所有 Agent 将在当前任务完成后停止，可随时恢复。',
        ()=>{ run.status='paused'; toast('warning','已暂停','运行已挂起'); renderDashboard(); })},
        iconPause(),'暂停'));
    }
    actions.push(h('button',{class:'btn btn-danger',onclick:()=>confirmAction('取消运行','取消后本次运行将被终止，未完成任务标记为失败。',
      async ()=>{
        if (isReal && run.dir) {
          try { await window.studio.cancelOffline(run.dir); toast('info','已发送取消','等待进程退出…'); }
          catch(e) { toast('error','取消失败',String(e.message||e)); }
        } else {
          run.status='cancelled'; pushEvent('run_cancelled',{}); stopSim(); renderDashboard();
          toast('error','已取消','运行已终止');
        }
      })},
      iconX(),'取消'));
  } else if (run.status==='paused' || run.status==='cancelled' || run.status==='failed'){
    actions.push(h('button',{class:'btn btn-primary',onclick:async ()=>{
      if (isReal && run.dir) {
        try {
          await window.studio.resumeRun({runDir: run.dir, mock: true, auto: true});
          toast('success','已恢复','从检查点继续运行');
          setTimeout(async ()=>{ try { await switchRun(run.dir); } catch {} }, 2000);
        } catch(e) { toast('error','恢复失败',String(e.message||e)); }
      } else {
        run.status='running'; pushEvent('human_approved',{note:'resume from checkpoint'}); startSim(); renderDashboard();
        toast('success','已恢复','从检查点继续运行');
      }
    }},
      iconPlay(),'恢复'));
  }
  if (isReal) {
    actions.push(h('button',{class:'btn',onclick:()=>{ openNewRunModal(); $('#nr-goal').value = run.project_goal || ''; }},'重新运行'));
    actions.push(h('button',{class:'btn',onclick:openModelSwitcher},'切换模型'));
    actions.push(h('button',{class:'btn',onclick:()=>window.studio.openPath(run.dir)},'打开目录'));
    actions.push(h('button',{class:'btn',onclick:exportRun},'导出运行'));
  } else {
    actions.push(h('button',{class:'btn',onclick:()=>toast('info','重新运行','已用相同目标启动新运行')},'重新运行'));
    actions.push(h('button',{class:'btn',onclick:()=>{ pushEvent('checkpoint_saved',{manual:true}); toast('success','检查点','已从最近检查点继续'); }},'从检查点继续'));
  }

  host.innerHTML='';
  host.appendChild(h('div',{},
    h('div',{class:'rs-top'},
      h('span',{class:'rs-id',title:'点击复制 Run ID',onclick:(e)=>{navigator.clipboard.writeText(run.run_id);toast('success','已复制',run.run_id);}},run.run_id),
      h('span',{class:'rs-goal',title:run.project_goal}, simplifyGoal(run.project_goal)),
      h('div',{class:'rs-stage-seg'}, PHASES.map((p,i)=>h('button',{class:(['planning','execution','delivery'][i])===run.phase?'on':'',disabled:true},p))),
      h('span',{class:`rs-status-chip ${meta.chip}`}, h('span',{class:`dot ${meta.dot}`}), meta.label)),
    h('div',{class:'rs-stats'},
      rsStat('已运行时间', elapsed!=null?fmtDur(elapsed):'—'),
      rsStat('Token 用量', fmtTokens((stats.prompt_tokens||0)+(stats.completion_tokens||0))),
      rsStat('估算费用', `${fmtCost(cost)} / $${run.budget_limit_usd}`),
      rsStat('运行模型', run.model),
      rsStat('任务进度', `${stats.succeeded||0}/${stats.total_tasks||0} 完成`),
      rsStat('最近更新', relTime(run.updated_at))),
    h('div',{class:'rs-actions'}, actions)));
}
function rsStat(k,v){ return h('div',{class:'rs-stat'},h('div',{class:'k'},k),h('div',{class:'v'},v)); }
/* 从完整项目目标中提取简洁标题：取冒号/句号前的核心短语 */
function simplifyGoal(goal){
  if(!goal) return '未命名项目';
  let s=goal.trim();
  // 优先取第一个冒号前（中英文冒号）
  const colonIdx=Math.min(...[s.indexOf('：'),s.indexOf(':')].filter(i=>i>=0).concat([Infinity]));
  if(colonIdx<Infinity && colonIdx>4) s=s.substring(0,colonIdx);
  else{
    // 否则取第一句话
    const stopIdx=Math.min(...[s.indexOf('。'),s.indexOf('. '),s.indexOf('！'),s.indexOf('!')].filter(i=>i>=0).concat([Infinity]));
    if(stopIdx<Infinity && stopIdx>6) s=s.substring(0,stopIdx);
  }
  s=s.trim();
  // 兜底：超过 28 字符截断
  return s.length>28 ? s.substring(0,28)+'…' : s;
}
function iconPause(){ return h('svg',{width:13,height:13,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':2,'stroke-linecap':'round'},h('path',{d:'M9 5v14M15 5v14'})); }
function iconX(){ return h('svg',{width:13,height:13,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':2},h('circle',{cx:12,cy:12,r:9}),h('path',{d:'M15 9l-6 6M9 9l6 6'})); }
function iconPlay(){ return h('svg',{width:13,height:13,viewBox:'0 0 24 24',fill:'currentColor'},h('path',{d:'M7 5l12 7-12 7z'})); }

function renderAgents() {
  const grid=$('#agents-grid'); if(!grid)return; grid.innerHTML='';
  const agents=S.dash.agents;
  const order=['project_manager','product_manager','architect','backend_dev','frontend_dev','qa_engineer','devops_engineer','technical_writer'];
  for(const rid of order){ const a=agents[rid]; const rm=roleMeta(rid);
    const active=S.detail&&S.detail.kind==='agent'&&S.detail.id===rid;
    grid.appendChild(h('div',{class:`agent-card${active?' active':''}`,onclick:()=>openAgentDetail(rid)},
      h('div',{class:'ac-head'},
        h('span',{class:'ac-dot',style:`background:${rm.color}`}),
        h('span',{class:'ac-name'},rm.full),
        h('span',{class:'ac-status'}, agentStatusLabel(a.status))),
      h('div',{class:'ac-task',title:a.task},a.task),
      h('div',{class:'ac-action',title:a.action},a.action),
      h('div',{class:'ac-meta'},
        metaCell('完成',String(a.done)), metaCell('失败/重试',String(a.fail)),
        metaCell('模型',a.model), metaCell('Token',fmtTokens(a.tokens)),
        metaCell('费用',fmtCost(a.cost)), metaCell('更新',relTime(a.updated)))));
  }
}
function metaCell(k,v){ return h('div',{},h('div',{style:'color:#8E8E93'},k),h('b',{},v)); }
function agentStatusLabel(st){ return {queued:'等待派发',executing:'执行中',reviewing:'评审中',done:'空闲'}[st]||st; }

function renderGates() {
  const grid=$('#gates-grid'); if(!grid)return; grid.innerHTML='';
  for(const g of S.dash.gates){
    const c={pending:'pending',running:'running',passed:'passed',failed:'failed',review:'review'}[g.status]||'pending';
    const failDetail=g.status==='failed'?h('div',{class:'gate-fail-detail'},
      h('div',{},h('span',{class:'lbl'},'失败原因：'),g.fail_reason||'关键检查未通过'),
      h('div',{},h('span',{class:'lbl'},'影响范围：'),g.impact||g.task_ref),
      h('div',{},h('span',{class:'lbl'},'建议处理：'),g.suggestion||'修复后重新提交'),
      h('div',{},h('span',{class:'lbl'},'返回节点：'),g.return_task||'T-11'),
      h('div',{},h('span',{class:'lbl'},'预计成本：'),g.extra_cost||'$0.05')):null;
    grid.appendChild(h('div',{class:`gate-card ${g.status==='failed'?'failed':g.status==='running'?'running':''}`},
      h('div',{class:'gate-head'},
        h('span',{class:'gate-num'},g.id),
        h('span',{class:'gate-name'},g.name),
        h('span',{class:`gate-status-chip ${c}`},gateStatusLabel(g.status)),
        h('div',{class:'gate-tags'},
          g.rework?h('span',{class:'gate-tag'},'允许返工'):h('span',{class:'gate-tag'},'不可返工'),
          g.need_human?h('span',{class:'gate-tag'},'需人工确认'):null)),
      h('div',{class:'gate-summary'},
        `检查项 ${g.checks.total} · 通过 ${g.checks.passed} · 失败 ${g.checks.failed}`+
        (g.duration_ms!=null?` · 耗时 ${fmtDur(g.duration_ms)}`:'')+` — ${g.verdict}`),
      failDetail,
      h('div',{class:'gate-actions'},
        h('button',{class:'btn btn-sm',onclick:()=>toast('info','门禁详情',`${g.id} 检查项：${g.checks.total} 项`)},'查看详情'),
        h('button',{class:'btn btn-sm btn-primary',onclick:()=>acceptGate(g)},'接受结果'),
        h('button',{class:'btn btn-sm btn-danger',onclick:()=>rejectGate(g)},'驳回并返工'),
        h('button',{class:'btn btn-sm',onclick:()=>{g.status='running';g.checks={total:g.checks.total,passed:0,failed:0};toast('info','重试门禁',g.name+' 重新运行');renderDashboard();}},'重试门禁'),
        h('button',{class:'btn btn-sm',onclick:()=>{const t=(g.task_ref||'').split(',')[0].trim();if(t)openTaskDetail(t);}},'相关任务'))));
  }
}
function gateStatusLabel(st){ return {pending:'未开始',running:'进行中',passed:'通过',failed:'失败',review:'等待人工确认'}[st]||st; }
function acceptGate(g){
  if (S.realMode && S.dash.run?.dir) {
    window.studio.gateAction(S.dash.run.dir, g.layer, 'approve')
      .then(() => { g.status='passed'; g.checks={...g.checks,failed:0}; pushEvent('gate_result',{layer:g.layer,verdict:'approve',gate:g.id}); toast('success','门禁通过',g.name+' 已接受'); renderDashboard(); })
      .catch(e => toast('error','批准失败',String(e.message||e)));
  } else {
    g.status='passed'; g.checks={...g.checks,failed:0}; pushEvent('gate_result',{layer:g.layer,verdict:'approve',gate:g.id}); toast('success','门禁通过',g.name+' 已接受'); renderDashboard();
  }
}
function rejectGate(g){ confirmAction('驳回并返工',`将 ${g.name} 打回返工，预计增加约 $0.05 模型成本。`,
  ()=>{
    if (S.realMode && S.dash.run?.dir) {
      window.studio.gateAction(S.dash.run.dir, g.layer, 'reject')
        .then(() => { g.status='failed'; pushEvent('gate_result',{layer:g.layer,verdict:'reject',gate:g.id}); toast('warning','已驳回',g.name+' 已退回相关任务'); renderDashboard(); })
        .catch(e => toast('error','驳回失败',String(e.message||e)));
    } else {
      g.status='failed'; pushEvent('gate_result',{layer:g.layer,verdict:'reject',gate:g.id}); toast('warning','已驳回',g.name+' 已退回相关任务'); renderDashboard();
    }
  }); }

function renderCostPanel() {
  const panel = $('#cost-panel'); if (!panel) return;
  const run = S.dash.run, st = run?.stats || {}, tasks = S.dash.tasks || [];
  const totalCost = st.estimated_cost_usd || 0;
  const budget = run?.budget_limit_usd || 1;
  const pct = Math.min(100, totalCost / budget * 100);
  const promptTokens = st.prompt_tokens || 0;
  const completionTokens = st.completion_tokens || 0;
  const llmCalls = st.llm_calls || 0;

  // 按 Agent 汇总成本
  const byAgent = {};
  for (const t of tasks) {
    const role = t.role || 'unknown';
    const cost = (t.result && t.result.cost_usd) || 0;
    const toks = (t.result && t.result.tokens) || 0;
    if (!byAgent[role]) byAgent[role] = { cost: 0, tokens: 0, count: 0 };
    byAgent[role].cost += cost;
    byAgent[role].tokens += toks;
    byAgent[role].count++;
  }
  const agentEntries = Object.entries(byAgent).sort((a, b) => b[1].cost - a[1].cost);
  const maxAgentCost = Math.max(...agentEntries.map(e => e[1].cost), 0.001);

  // Top 5 最贵任务
  const topTasks = [...tasks]
    .filter(t => t.result && t.result.cost_usd)
    .sort((a, b) => (b.result.cost_usd || 0) - (a.result.cost_usd || 0))
    .slice(0, 5);

  panel.innerHTML = '';
  panel.appendChild(h('div', { class: 'cost-grid' },
    // 总览卡片
    h('div', { class: 'cost-overview' },
      h('div', { class: 'cost-row' },
        h('div', { class: 'cost-big' }, `$${totalCost.toFixed(4)}`),
        h('div', { class: 'cost-sub' }, `预算 $${budget.toFixed(2)} · ${Math.round(pct)}%`)),
      h('div', { class: 'cost-bar' },
        h('div', { class: `cost-bar-fill ${pct >= 100 ? 'err' : pct >= 90 ? 'warn' : ''}`, style: `width:${pct}%` })),
      h('div', { class: 'cost-meta' },
        h('span', {}, `LLM 调用 ${llmCalls} 次`),
        h('span', {}, `输入 ${fmtTokens(promptTokens)}`),
        h('span', {}, `输出 ${fmtTokens(completionTokens)}`))),
    // 按 Agent 分布
    h('div', { class: 'cost-by-agent' },
      h('div', { class: 'cost-section-title' }, '按 Agent 分布'),
      ...agentEntries.map(([role, d]) => {
        const meta = ROLE_META[role] || { label: role, color: '#8E8E93' };
        const w = (d.cost / maxAgentCost * 100).toFixed(1);
        return h('div', { class: 'cost-agent-row' },
          h('span', { class: 'cost-agent-name', style: `color:${meta.color}` }, meta.label || role),
          h('div', { class: 'cost-agent-bar' }, h('div', { class: 'cost-agent-fill', style: `width:${w}%;background:${meta.color}` })),
          h('span', { class: 'cost-agent-val' }, `$${d.cost.toFixed(4)}`));
      }),
      agentEntries.length === 0 ? h('div', { class: 'cost-empty' }, '暂无成本数据') : null),
    // Top 5 任务
    h('div', { class: 'cost-top-tasks' },
      h('div', { class: 'cost-section-title' }, '最贵任务 Top 5'),
      ...topTasks.map(t => h('div', { class: 'cost-task-row', onclick: () => openTaskDetail(t.id) },
        h('span', { class: 'cost-task-id' }, t.id),
        h('span', { class: 'cost-task-title' }, trunc(t.title, 24)),
        h('span', { class: 'cost-task-val' }, `$${(t.result.cost_usd || 0).toFixed(4)}`))),
      topTasks.length === 0 ? h('div', { class: 'cost-empty' }, '暂无任务成本数据') : null)));
}

function renderEvents() {
  const list=$('#events-list'), filter=$('#events-filter'); if(!list)return;
  const mainTypes=['run_started','plan_created','task_started','task_finished','gate_result','llm_failover','budget_warning','checkpoint_saved','artifact_written','human_approved','error'];
  filter.innerHTML='';
  for(const ty of mainTypes){ const m=EVENT_META[ty];
    filter.appendChild(h('button',{class:`fchip${S.eventFilter.has(ty)?' on':''}`,
      style:`--ev-color:${m.color};--ev-soft:${m.color}22`,
      onclick:()=>{ S.eventFilter.has(ty)?S.eventFilter.delete(ty):S.eventFilter.add(ty); renderEvents(); }},
      h('span',{class:'fdot'}), m.label));
  }
  list.innerHTML='';
  const evs=[...S.dash.events].reverse().filter(e=>S.eventFilter.has(e.event)).slice(0,50);
  for(const e of evs){
    const m=EVENT_META[e.event]||{color:'#8E8E93',label:e.event};
    const key=e.ts+e.event; const expanded=S.eventExpanded.has(key);
    const tid=e.data&&e.data.task_id;
    list.appendChild(h('div',{class:`event-row${['error','gate_failed'].includes(e.event)?' ev-error':''}`},
      h('span',{class:'event-ts'},relTime(e.ts)),
      h('span',{class:'event-type',style:`color:${m.color}`},m.label),
      h('div',{class:'event-body'},
        h('div',{class:'event-text'}, eventSummary(e),
          tid?h('span',{class:'event-task-link',onclick:()=>openTaskDetail(tid)},` ${tid}`):null,
          h('span',{class:'event-toggle',onclick:(ev2)=>{ev2.stopPropagation(); expanded?S.eventExpanded.delete(key):S.eventExpanded.add(key); renderEvents();}}, expanded?'收起':'详情')),
        expanded?h('div',{class:'event-payload'},h('pre',{},JSON.stringify(e.data,null,2))):null)));
  }
}
function eventSummary(e) {
  const d=e.data||{};
  switch(e.event){
    case 'run_started': return `运行创建：${trunc(d.goal||'',40)}`;
    case 'plan_created': return `任务生成：${d.task_count} 个任务`;
    case 'task_started': return `${d.task_id||''} 开始执行（${roleMeta(d.role).full}，第 ${d.attempt||1} 次）`;
    case 'task_finished': return `${d.task_id||''} 完成，耗时 ${fmtDur(d.duration_ms)}`;
    case 'task_transition': return `${d.task_id||''} ${d.from} → ${d.to}（${d.reason||''}）`;
    case 'artifact_written': return `产物生成：${d.path}`;
    case 'gate_result': return `门禁 ${d.gate||d.layer}：${d.verdict}${d.score!=null?`（分数 ${d.score}）`:''}`;
    case 'llm_failover': return `Provider 故障转移：${d.from||'?'} → ${d.to||'?'}（${trunc(d.reason||'',30)}）`;
    case 'budget_warning': return `预算预警：已使用 ${d.pct}%`;
    case 'budget_exhausted': return '预算硬上限耗尽，运行优雅收尾';
    case 'task_timeout': return `${d.task_id||''} 超时`;
    case 'checkpoint_saved': return `检查点保存${d.manual?'（手动）':''}`;
    case 'human_approved': return `人工批准：${trunc(d.note||'',40)}`;
    case 'error': return `${d.where||''}：${trunc(d.message||'',50)}`;
    default: return JSON.stringify(d).slice(0,80);
  }
}

/* ============================================================
   右侧详情面板
   ============================================================ */
function openDetailPanel(node){
  const p=$('#detail-panel'); p.innerHTML=''; p.appendChild(node);
  $('#detail-mask').classList.remove('hidden'); $('#detail-mask').classList.add('show');
  p.classList.remove('hidden'); p.classList.add('show');
}
function closeDetailPanel(){
  const p=$('#detail-panel'); p.classList.remove('show'); $('#detail-mask').classList.remove('show');
  setTimeout(()=>{p.classList.add('hidden');$('#detail-mask').classList.add('hidden');},220);
}
$('#detail-mask').addEventListener('click',closeDetailPanel);
document.addEventListener('keydown',(e)=>{ if(e.key==='Escape'){ closeDetailPanel(); closeModal(); } });

function openTaskDetail(taskId){
  const t=S.dash.tasks.find(x=>x.id===taskId); if(!t)return;
  S.detail={kind:'task',id:taskId};
  const meta=STATUS_META[t.status]||STATUS_META.unknown;
  const rm=roleMeta(t.role);
  const up=(t.dependencies||[]).map(d=>S.dash.tasks.find(x=>x.id===d)).filter(Boolean);
  const down=S.dash.tasks.filter(x=>(x.dependencies||[]).includes(t.id));
  const res=t.result;
  openDetailPanel(h('div',{style:'display:flex;flex-direction:column;height:100%'},
    h('div',{class:'dp-head'},
      h('div',{class:'dp-title-row'},
        h('div',{},
          h('div',{class:'mono',style:`color:${meta.color};font-size:12px`},t.id),
          h('div',{class:'dp-title'},t.title)),
        h('button',{class:'btn btn-icon',style:'width:28px;height:28px','aria-label':'关闭',title:'关闭',onclick:closeDetailPanel},'✕'))),
    h('div',{class:'dp-body'},
      sec('状态', h('span',{class:'dp-tag',style:`color:${meta.color}`},meta.label),
        h('span',{class:'dp-tag'},`阶段：${phaseLabel(S.dash.run.phase)}`)),
      sec('负责 Agent', h('span',{class:'dp-tag'},h('span',{class:'dot',style:`background:${rm.color};display:inline-block;margin-right:4px`}),rm.full)),
      t.brief?sec('一句话摘要',h('div',{class:'dp-text dim'},t.brief)):null,
      sec('描述',h('div',{class:'dp-text dim'},t.description||'（无描述）')),
      sec('上游依赖',up.length?h('div',{class:'dep-list'},up.map(u=>h('span',{class:'dp-dep-link',onclick:()=>openTaskDetail(u.id)},`${u.id} ${trunc(u.title,10)}`))):h('div',{class:'dp-text dim'},'无')),
      sec('下游依赖',down.length?h('div',{class:'dep-list'},down.map(u=>h('span',{class:'dp-dep-link',onclick:()=>openTaskDetail(u.id)},`${u.id} ${trunc(u.title,10)}`))):h('div',{class:'dp-text dim'},'无')),
      sec('执行配置',h('div',{class:'dp-meta-grid'},
        metaCell('尝试次数',`${t.attempts||1}/${t.max_attempts}`),
        metaCell('返工次数',`${t.rework_count||0}/${t.max_reworks}`),
        metaCell('超时',`${t.timeout_seconds||300}s`),
        metaCell('优先级',`P${t.priority||5}`))),
      sec('输出产物', res&&res.artifacts&&res.artifacts.length
        ? h('div',{},res.artifacts.map(a=>h('div',{class:'dp-text',style:'margin:2px 0'},`📄 ${a.path} · ${fmtBytes(a.size_bytes)}`)))
        : h('div',{class:'dp-text dim'},'待生成')),
      t.error?sec('最近错误',h('div',{class:'dp-text',style:'color:#FF3B30'},t.error)):null,
      res&&(res.tokens_used||res.duration_ms)?sec('执行统计',h('div',{class:'dp-meta-grid'},
        metaCell('Token',fmtTokens(res.tokens_used||0)),
        metaCell('耗时',fmtDur(res.duration_ms||0)),
        metaCell('费用',res.cost_usd?`$${res.cost_usd.toFixed(4)}`:'—'),
        metaCell('模型',res.model||'—'))):null,
      t.reviews&&t.reviews.length?sec('门禁评审',h('div',{class:'dp-reviews'},
        t.reviews.map(r=>h('div',{class:'dp-review-item'},
          h('span',{class:`dp-review-verdict ${r.verdict}`},r.verdict),
          h('span',{class:'dp-review-layer'},r.layer||''),
          h('span',{class:'dp-review-text'},trunc(r.summary||(r.issues&&r.issues.join('; '))||'',60)))))):null,
      t.status_history&&t.status_history.length?sec('状态历史',h('div',{class:'dp-status-history'},
        t.status_history.slice(-8).map(sh=>h('div',{class:'dp-sh-item'},
          h('span',{class:'dp-sh-status'},sh.status||''),
          h('span',{class:'dp-sh-time'},fmtClock(sh.at||sh.time||sh.timestamp||'')))))):null,
      sec('运行日志',h('div',{class:'dp-log'},
        h('div',{},`$ running ${t.id}`),
        h('div',{},`> role=${rm.full} model=${S.dash.run.model}`),
        res&&res.summary?h('div',{},`> ${trunc(res.summary,80)}`):h('div',{},'> loading context…'),
        t.status==='failed'?h('div',{class:'err'},`✗ ${t.error||'失败'}`):h('div',{},'> done')))),
    h('div',{class:'dp-actions'},
      S.realMode
        ? h('div',{class:'dp-text dim',style:'font-size:11px'},'任务级操作需通过 CLI 或 API 执行')
        : h('div',{style:'display:flex;gap:6px;flex-wrap:wrap'},
            h('button',{class:'btn btn-sm',onclick:async()=>{
              if (S.realMode && S.dash.run?.dir) { try { await window.studio.taskAction(S.dash.run.dir, t.id, 'retry'); toast('success','已重试',t.id); } catch(e){ toast('error','重试失败',String(e.message||e)); } }
              else { t.status='ready'; t.attempts=(t.attempts||0)+1; toast('info','重试任务',t.id); }
              renderDashboard();
            }},'重试任务'),
            h('button',{class:'btn btn-sm',onclick:async()=>{
              if (S.realMode && S.dash.run?.dir) { try { await window.studio.taskAction(S.dash.run.dir, t.id, 'skip'); toast('success','已跳过',t.id); } catch(e){ toast('error','跳过失败',String(e.message||e)); } }
              else { t.status='skipped'; toast('info','已跳过',t.id); }
              renderDashboard();
            }},'跳过任务'),
            h('button',{class:'btn btn-sm',onclick:async()=>{
              if (S.realMode && S.dash.run?.dir) { try { await window.studio.taskAction(S.dash.run.dir, t.id, 'manual'); toast('success','已标记',t.id+' 人工处理'); } catch(e){ toast('error','标记失败',String(e.message||e)); } }
              else { t.status='review'; toast('info','人工处理',t.id+' 标记为人工处理'); }
              renderDashboard();
            }},'人工处理'),
            h('button',{class:'btn btn-sm btn-primary',onclick:()=>toast('info','恢复运行','从 '+t.id+' 恢复')},'从此恢复')))));
  renderAgents();
}
function openAgentDetail(roleId){
  const a=S.dash.agents[roleId]; if(!a)return;
  S.detail={kind:'agent',id:roleId};
  const rm=roleMeta(roleId);
  const myTasks=S.dash.tasks.filter(t=>t.role===roleId);
  openDetailPanel(h('div',{style:'display:flex;flex-direction:column;height:100%'},
    h('div',{class:'dp-head'},h('div',{class:'dp-title-row'},
      h('div',{},h('div',{class:'mono',style:'color:#8E8E93;font-size:12px'},'AGENT'),h('div',{class:'dp-title'},rm.full)),
      h('button',{class:'btn btn-icon',style:'width:28px;height:28px','aria-label':'关闭',title:'关闭',onclick:closeDetailPanel},'✕'))),
    h('div',{class:'dp-body'},
      sec('状态',h('span',{class:'dp-tag'},agentStatusLabel(a.status)),h('span',{class:'dp-tag'},a.model)),
      sec('当前任务',h('div',{class:'dp-text'},a.task)),
      sec('最近动作',h('div',{class:'dp-text dim'},a.action)),
      sec('统计',h('div',{class:'dp-meta-grid'},
        metaCell('已完成',String(a.done)),metaCell('失败/重试',String(a.fail)),
        metaCell('Token',fmtTokens(a.tokens)),metaCell('费用',fmtCost(a.cost)))),
      sec('负责任务列表', myTasks.length?h('div',{style:'display:flex;flex-direction:column;gap:4px'},
        myTasks.map(t=>{const m=STATUS_META[t.status]||STATUS_META.unknown;
          return h('div',{class:'dag-task-row',onclick:()=>openTaskDetail(t.id)},
            h('span',{class:'dot',style:`background:${m.color}`}),
            h('span',{class:'mono',style:'color:#8E8E93'},t.id),
            h('span',{},trunc(t.title,16)),
            h('span',{style:`margin-left:auto;font-size:11px;color:${m.color}`},m.label));}))
        :h('div',{class:'dp-text dim'},'暂无任务'))),
    h('div',{class:'dp-actions'},
      h('button',{class:'btn btn-sm',onclick:()=>toast('info','查看事件',rm.full+' 的事件')},'查看事件'),
      h('button',{class:'btn btn-sm',onclick:()=>toast('info','查看产物',rm.full+' 的产物')},'查看产物'))));
  renderAgents();
}
function phaseLabel(p){ return {planning:'规划',execution:'执行',delivery:'交付'}[p]||p; }
function sec(title,...kids){ return h('div',{class:'dp-section'},h('h4',{},title),...kids); }

/* ============================================================
   模拟实时更新
   ============================================================ */
function pushEvent(event, data, taskId, role){
  S.dash.events.push({ts:nowTs(0), run_id:RUN_ID, event, data:Object.assign(taskId?{task_id:taskId}:{},role?{role}:{},data||{})});
  window.__st.ledgerBatches += 1;
}
function startSim(){ if(S.simTimer)return; S.simTimer=setInterval(simTick,2500); }
function stopSim(){ if(S.simTimer){clearInterval(S.simTimer);S.simTimer=null;} }

function simTick(){
  const d=S.dash; if(!d)return;
  const run=d.run; if(run.status!=='running')return;
  window.__st.snapshots += 1;

  for(const t of d.tasks){
    if(t.status==='running'){
      t.progress=Math.min(100,(t.progress||0)+15+Math.random()*12);
      if(t.progress>=100){
        // T-08 第一次失败后重试：running → ready（reason:retry），attempts+1
        if(t.id==='T-08' && t.attempts===1){
          t.status='ready'; t.error='连接数据库超时（ETIMEDOUT）'; t.attempts=2; t.progress=0;
          pushEvent('task_transition',{from:'running',to:'ready',reason:'retry: ETIMEDOUT'} ,t.id,t.role);
          pushEvent('error',{where:t.id,message:'ETIMEDOUT'},t.id,t.role);
          toast('error','任务失败将重试',`${t.id} 第 ${t.attempts} 次尝试`);
        } else {
          t.status='review'; t.progress=0;
          const dur=300000+Math.random()*400000;
          t.result={summary:'任务产物已生成并通过初检', duration_ms:dur, artifacts:[{path:`artifacts/${t.id}/output.md`,size_bytes:2000+Math.random()*8000}]};
          pushEvent('task_finished',{attempt:t.attempts,duration_ms:dur},t.id,t.role);
          // 门禁：G1 static approve → succeeded
          t.reviews=[{round:1,layer:'static',verdict:'approve',score:null,reviewer:'static:contract'}];
          pushEvent('gate_result',{task_id:t.id,round:1,layer:'static',verdict:'approve',score:null});
          t.status='succeeded';
          pushEvent('task_transition',{from:'review',to:'succeeded',reason:'gate-succeeded'},t.id,t.role);
          // 解除下游
          for(const down of d.tasks){
            if((down.dependencies||[]).includes(t.id)){
              const deps=down.dependencies.map(x=>d.tasks.find(z=>z.id===x));
              if(deps.every(z=>z.status==='succeeded') && down.status==='blocked'){
                down.status='ready'; pushEvent('task_transition',{from:'blocked',to:'ready',reason:'dependencies-recovered'},down.id,down.role);
              }
            }
          }
        }
      }
    } else if(t.status==='ready'){
      t.status='running'; t.progress=5;
      const ag=d.agents[t.role]; if(ag){ag.status='executing';ag.task=`${t.id} ${t.title}`;ag.action='正在执行';ag.updated=nowTs(0);}
      pushEvent('task_started',{attempt:t.attempts},t.id,t.role);
    }
  }
  // 依赖满足：pending → ready；上游失败 → blocked
  for(const t of d.tasks){
    if(t.status==='pending'||t.status==='blocked'){
      const deps=(t.dependencies||[]).map(x=>d.tasks.find(z=>z.id===x)).filter(Boolean);
      if(!deps.length){ if(t.status==='pending')t.status='ready'; }
      else{
        const allOk=deps.every(z=>z.status==='succeeded');
        const anyFail=deps.some(z=>['failed','cancelled'].includes(z.status));
        if(allOk) t.status='ready';
        else if(anyFail) t.status='blocked';
      }
    }
  }
  // 统计递增
  const st=run.stats;
  st.llm_calls+=2; st.prompt_tokens+=Math.round(1500+Math.random()*2000);
  st.completion_tokens+=Math.round(400+Math.random()*800);
  st.estimated_cost_usd+=0.004+Math.random()*0.006;
  st.succeeded=d.tasks.filter(t=>t.status==='succeeded').length;
  run.updated_at=nowTs(0);

  const pct=st.estimated_cost_usd/run.budget_limit_usd*100;
  if(pct>=90 && !d._budgetWarned){ d._budgetWarned=true; toast('warning','预算预警',`已使用 ${Math.round(pct)}%`); pushEvent('budget_warning',{pct:Math.round(pct)}); }

  const unfinished=d.tasks.filter(t=>!['succeeded','failed','skipped','cancelled'].includes(t.status));
  if(unfinished.length===0){
    run.status='completed'; run.phase='delivery';
    pushEvent('run_finished',{status:'completed',exit_code:0,duration_ms:Date.now()-parseTs(st.started_at).getTime()});
    toast('success','运行完成','所有任务已通过，产物已就绪'); stopSim();
  }
  renderDashboard();
}

/* ============================================================
   导航 & 占位页
   ============================================================ */
const PLACEHOLDERS={
  tasks:{title:'任务图',desc:'完整 DAG 编辑器与任务调试视图正在开发中。当前请使用上方运行总览中的依赖图。'},
  agents:{title:'Agents',desc:'Agent 编排与提示词配置面板正在开发中。当前请使用上方的 Agent 协作状态卡片。'},
  gates:{title:'质量门禁',desc:'门禁规则配置与历史报告正在开发中。当前请使用上方的三层质量门禁概览。'},
  artifacts:{title:'产物',desc:'产物浏览器与预览正在开发中。运行完成后 artifacts/ 目录将自动填充交付物。'},
  events:{title:'事件日志',desc:'完整 ledger 搜索与过滤正在开发中。当前请使用上方的最近事件时间线。'},
  settings:{title:'设置',desc:'连接设置、预算策略与主题配置正在开发中。'},
};
/* ============================================================
   产物页
   ============================================================ */
const ARTIFACT_ICONS = {
  code: '📄', doc: '📄', test: '🧪', report: '📊', config: '⚙️', other: '📦',
};

async function renderArtifactsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '产物'),
    h('div', { style: 'display:flex;gap:8px' },
      h('button', { class: 'btn', onclick: async () => {
        if (!S.dash.run || !S.dash.run.dir) return;
        try { await window.studio.exportArtifacts(S.dash.run.dir); toast('success', '已导出', '产物已打包为 ZIP'); }
        catch (e) { toast('error', '导出失败', String(e.message || e)); }
      } }, '导出 ZIP'),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  const body = h('div', { class: 'art-body' });
  container.appendChild(body);

  if (!S.dash.run || !S.dash.run.dir) {
    body.appendChild(h('div', { class: 'ph-empty' }, '未选择运行'));
    return;
  }

  // 左侧文件树
  const tree = h('div', { class: 'art-tree' });
  const preview = h('div', { class: 'art-preview' });
  body.appendChild(tree); body.appendChild(preview);

  tree.appendChild(h('div', { class: 'art-tree-head' }, '文件列表'));
  const listEl = h('div', { class: 'art-tree-list' });
  tree.appendChild(listEl);

  let entries = [];
  try {
    entries = await window.studio.listArtifacts(S.dash.run.dir);
  } catch (e) {
    listEl.appendChild(h('div', { class: 'ph-empty' }, '加载失败：' + String(e.message || e)));
    return;
  }

  if (!entries || !entries.length) {
    listEl.appendChild(h('div', { class: 'ph-empty' }, '该运行暂无产物'));
    preview.appendChild(h('div', { class: 'ph-empty' }, '选择左侧文件查看内容'));
    return;
  }

  // 文件统计摘要
  const totalSize = entries.reduce((s, e) => s + (e.sizeBytes || 0), 0);
  const kinds = {};
  for (const e of entries) { const k = e.kind || 'other'; kinds[k] = (kinds[k] || 0) + 1; }
  const summary = h('div', { class: 'art-summary' },
    h('span', {}, `${entries.length} 个文件`),
    h('span', { class: 'art-summary-sep' }, '·'),
    h('span', {}, fmtBytes(totalSize)),
    ...Object.entries(kinds).map(([k, v]) => [h('span', { class: 'art-summary-sep' }, '·'), h('span', {}, `${ARTIFACT_ICONS[k] || '📄'} ${v}`)]).flat());
  tree.insertBefore(summary, listEl);

  // 按目录分组
  const groups = {};
  for (const e of entries) {
    const dir = (e.path || '').split('/')[0] || '根目录';
    if (!groups[dir]) groups[dir] = [];
    groups[dir].push(e);
  }

  for (const [dir, files] of Object.entries(groups)) {
    listEl.appendChild(h('div', { class: 'art-dir' }, dir));
    for (const f of files) {
      const icon = ARTIFACT_ICONS[f.kind] || '📄';
      const item = h('div', { class: 'art-file', onclick: async () => {
        $$('.art-file').forEach(x => x.classList.remove('active'));
        item.classList.add('active');
        await previewArtifact(preview, f);
      }},
        h('span', { class: 'art-file-icon' }, icon),
        h('span', { class: 'art-file-name', title: f.path }, (f.path || '').split('/').pop()),
        h('span', { class: 'art-file-size' }, fmtBytes(f.sizeBytes)));
      listEl.appendChild(item);
    }
  }
  preview.appendChild(h('div', { class: 'ph-empty' }, '选择左侧文件查看内容'));
}

async function previewArtifact(preview, f) {
  preview.innerHTML = '';
  preview.appendChild(h('div', { class: 'art-preview-head' },
    h('span', { class: 'art-preview-path' }, f.path || ''),
    h('span', { class: 'art-preview-meta' },
      `${fmtBytes(f.sizeBytes)} · ${f.role || '—'} · ${f.task || '—'}`),
    h('div', { style: 'display:flex;gap:4px' },
      f.task ? h('button', { class: 'btn btn-sm', onclick: () => { closeTaskDetail(); openTaskDetail(f.task); } }, '关联任务') : null,
      h('button', { class: 'btn btn-sm', title: '在文件管理器中显示',
        onclick: () => { const p = S.dash.run.dir + '\\' + (f.path || '').replace(/\//g, '\\'); window.studio.revealFile(p); } },
        '📂 显示'))));
  const content = h('pre', { class: 'art-preview-content' });
  preview.appendChild(content);
  try {
    const r = await window.studio.readArtifact(S.dash.run.dir, f.path);
    const text = r && r.content != null ? String(r.content) : (r && typeof r === 'string' ? r : '');
    if (!text) { content.textContent = '（空文件或二进制）'; return; }
    const truncated = text.length > 100000 ? text.slice(0, 100000) + '\n…（已截断，共 ' + text.length + ' 字符）' : text;
    content.innerHTML = highlightCode(truncated, f.path || '');
  } catch (e) {
    content.textContent = '读取失败：' + String(e.message || e);
  }
}

/* 轻量语法高亮（零依赖，基于正则） */
function highlightCode(text, filename) {
  const ext = (filename.split('.').pop() || '').toLowerCase();
  const lang = ({
    py: 'python', js: 'javascript', ts: 'javascript', jsx: 'javascript', tsx: 'javascript',
    json: 'json', yaml: 'yaml', yml: 'yaml', md: 'markdown', markdown: 'markdown',
    html: 'html', htm: 'html', css: 'css', sh: 'shell', bash: 'shell',
    toml: 'toml', ini: 'ini', cfg: 'ini', xml: 'html', svg: 'html',
  })[ext] || 'text';
  let escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  if (lang === 'text') return escaped;
  const patterns = {
    python: [
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/#[^\n]*/g, 'tok-com'],
      [/\b(def|class|return|if|elif|else|for|while|import|from|as|try|except|finally|with|lambda|yield|raise|pass|break|continue|global|nonlocal|assert|del|in|is|not|and|or|True|False|None|async|await)\b/g, 'tok-kw'],
      [/\b(self|cls)\b/g, 'tok-self'],
    ],
    javascript: [
      [/(&quot;.*?&quot;|'.*?'|`.*?`)/g, 'tok-str'],
      [/\/\/[^\n]*/g, 'tok-com'],
      [/\/\*[\s\S]*?\*\//g, 'tok-com'],
      [/\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|super|this|import|export|from|default|try|catch|finally|throw|async|await|yield|typeof|instanceof|in|of|true|false|null|undefined)\b/g, 'tok-kw'],
    ],
    json: [
      [/&quot;([^&]+?)&quot;(\s*:)/g, '<span class="tok-key">&quot;$1&quot;</span>$2'],
      [/(&quot;.*?&quot;)/g, 'tok-str'],
      [/\b(true|false|null)\b/g, 'tok-kw'],
    ],
    yaml: [
      [/#[^\n]*/g, 'tok-com'],
      [/^(\s*[\w\-]+:)/gm, 'tok-key'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/\b(true|false|null|yes|no|on|off)\b/g, 'tok-kw'],
    ],
    markdown: [
      [/^(#{1,6}\s.+)$/gm, 'tok-md-h'],
      [/(\*\*[^*]+\*\*|__[^_]+__)/g, 'tok-md-bold'],
      [/(`[^`]+`)/g, 'tok-md-code'],
      [/^(\s*[-*+]\s)/gm, 'tok-md-list'],
    ],
    html: [
      [/(&lt;\/?[\w\-]+)/g, 'tok-tag'],
      [/([\w\-]+=)(&quot;.*?&quot;|'.*?')/g, '$1<span class="tok-str">$2</span>'],
      [/(&lt;!--[\s\S]*?--&gt;)/g, 'tok-com'],
    ],
    css: [
      [/\/\*[\s\S]*?\*\//g, 'tok-com'],
      [/([\w\-]+)\s*:/g, '<span class="tok-key">$1</span>:'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/(#[0-9a-fA-F]{3,8})\b/g, 'tok-num'],
    ],
    shell: [
      [/#[^\n]*/g, 'tok-com'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/\b(echo|cd|ls|mkdir|rm|cp|mv|cat|grep|sed|awk|export|source|if|then|else|fi|for|do|done|while|case|esac|function|return|exit)\b/g, 'tok-kw'],
    ],
    toml: [
      [/#[^\n]*/g, 'tok-com'],
      [/^(\s*[\w\-.]+\s*=)/gm, 'tok-key'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/\b(true|false)\b/g, 'tok-kw'],
    ],
    ini: [
      [/^(\[.*\])/gm, 'tok-key'],
      [/^(\s*[\w\-.]+\s*=)/gm, 'tok-key'],
      [/[;#][^\n]*/g, 'tok-com'],
    ],
  };
  const rules = patterns[lang] || [];
  for (const [pat, cls] of rules) {
    if (typeof cls === 'string') {
      escaped = escaped.replace(pat, (m) => `<span class="${cls}">${m}</span>`);
    } else {
      escaped = escaped.replace(pat, cls);
    }
  }
  return escaped;
}

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

/* ============================================================
   设置页
   ============================================================ */
async function renderSettingsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '设置'),
    h('button', { class: 'btn btn-primary', onclick: async () => {
      const next = {
        pythonCmd: $('#set-python').value.trim() || 'python',
        outputDir: $('#set-output').value.trim(),
        defaultModel: $('#set-model-custom').value.trim() || $('#set-model').value.trim(),
        maxCostUsd: parseFloat($('#set-maxcost').value) || 0,
      };
      try {
        await window.studio.setSettings(next);
        S.settings = Object.assign({}, S.settings, next);
        toast('success', '已保存', '设置已更新');
      } catch (e) { toast('error', '保存失败', String(e.message || e)); }
    }}, '保存')));

  const cfg = S.settings || {};
  const form = h('div', { class: 'settings-form' });
  container.appendChild(form);

  const field = (label, id, value, placeholder, type = 'text') =>
    h('div', { class: 'settings-field' },
      h('label', { for: id }, label),
      h('input', { id, type, value: value || '', placeholder: placeholder || '' }));

  form.appendChild(field('Python 命令', 'set-python', cfg.pythonCmd, 'python / python3 / 完整路径'));
  form.appendChild(field('输出目录', 'set-output', cfg.outputDir, 'E:\\agentcrew\\output'));
  form.appendChild(h('div', { class: 'settings-field' },
    h('label', { for: 'set-model' }, '默认模型'),
    h('div', { style: 'display:flex;gap:6px' },
      h('select', { id: 'set-model', style: 'flex:1;padding:6px 8px;border:1px solid rgba(60,60,67,.18);border-radius:6px;font-size:12px;font-family:inherit;background:#fff' },
        h('option', { value: '', selected: !cfg.defaultModel }, '选择模型…'),
        ...COMMON_MODELS.map(m => h('option', { value: m, selected: cfg.defaultModel === m }, m))),
      h('input', { id: 'set-model-custom', type: 'text', placeholder: '自定义',
        style: 'width:120px;padding:6px 8px;border:1px solid rgba(60,60,67,.18);border-radius:6px;font-size:12px;font-family:inherit' }))));
  form.appendChild(field('预算上限 (USD)', 'set-maxcost', cfg.maxCostUsd || '', '0 = 不限制', 'number'));

  form.appendChild(h('div', { class: 'settings-field' },
    h('label', {}, '关于'),
    h('div', { class: 'settings-about' },
      'AgentCrew Studio · 多 Agent 协作开发控制台',
      h('br'), '8 角色协作 · 三层门禁 · DAG 编排 · 实时可观测',
      h('br'), h('br'),
      h('span', { style: 'font-weight:600;color:#6E6E73' }, '快捷键'),
      h('br'), 'Esc — 关闭面板 / 弹窗',
      h('br'), 'Ctrl/⌘ + N — 新建运行',
      h('br'), '1-8 — 切换页面（总览/计划/任务图/Agents/门禁/产物/事件/设置）')));
}

/* ============================================================
   事件日志页（全量）
   ============================================================ */
function renderEventsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '事件日志'),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      h('input', { id: 'ev-search', type: 'text', placeholder: '搜索事件…',
        style: 'width:200px;padding:6px 10px;border:1px solid rgba(60,60,67,.18);border-radius:6px;font-size:12px;font-family:inherit',
        oninput: () => renderEventsList() }),
      h('button', { class: 'btn', onclick: () => exportEvents() }, '导出 JSON'),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  const filterBar = h('div', { class: 'ev-filter-bar', id: 'ev-filter-bar' });
  container.appendChild(filterBar);

  const listEl = h('div', { class: 'ev-full-list', id: 'ev-full-list' });
  container.appendChild(listEl);

  // 构建类型筛选
  const allTypes = [...new Set((S.dash.events || []).map(e => e.event))].sort();
  S.eventFilter = new Set(allTypes);
  for (const t of allTypes) {
    const btn = h('button', { class: 'ev-filter-btn active', 'data-type': t,
      onclick: () => {
        btn.classList.toggle('active');
        if (S.eventFilter.has(t)) S.eventFilter.delete(t); else S.eventFilter.add(t);
        renderEventsList();
      }}, t);
    filterBar.appendChild(btn);
  }

  renderEventsList();
}

function renderEventsList() {
  const listEl = $('#ev-full-list');
  if (!listEl) return;
  const q = ($('#ev-search')?.value || '').toLowerCase();
  const evs = [...(S.dash.events || [])].reverse()
    .filter(e => S.eventFilter.has(e.event))
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q));
  listEl.innerHTML = '';
  if (!evs.length) {
    listEl.appendChild(h('div', { class: 'ph-empty' }, '无匹配事件'));
    return;
  }
  const PAGE_SIZE = 100;
  const totalPages = Math.ceil(evs.length / PAGE_SIZE);
  if (!S.evPage || S.evPage > totalPages) S.evPage = 1;
  const start = (S.evPage - 1) * PAGE_SIZE;
  const pageEvs = evs.slice(start, start + PAGE_SIZE);
  for (const e of pageEvs) {
    const item = h('div', { class: 'ev-full-item' },
      h('div', { class: 'ev-full-main', onclick: () => item.classList.toggle('expanded') },
        h('span', { class: 'ev-full-ts' }, fmtClock(e.ts)),
        h('span', { class: 'ev-full-type' }, e.event),
        h('span', { class: 'ev-full-task' }, e.data?.task_id || ''),
        h('span', { class: 'ev-full-role' }, e.data?.role || ''),
        h('span', { class: 'ev-full-summary' }, summarizeEvent(e))));
    const detail = h('pre', { class: 'ev-full-detail' }, JSON.stringify(e, null, 2));
    item.appendChild(detail);
    listEl.appendChild(item);
  }
  // 分页控件
  if (totalPages > 1) {
    const pager = h('div', { class: 'ev-pager' },
      h('button', { class: 'btn btn-sm', disabled: S.evPage <= 1, onclick: () => { S.evPage--; renderEventsList(); } }, '上一页'),
      h('span', { class: 'ev-pager-info' }, `第 ${S.evPage} / ${totalPages} 页 · 共 ${evs.length} 条`),
      h('button', { class: 'btn btn-sm', disabled: S.evPage >= totalPages, onclick: () => { S.evPage++; renderEventsList(); } }, '下一页'));
    listEl.appendChild(pager);
  }
}

/* 导出事件为 JSON 文件下载 */
function exportEvents() {
  const events = S.dash.events || [];
  if (!events.length) { toast('warning', '无事件', '当前运行没有事件记录'); return; }
  const data = JSON.stringify({ run_id: S.dash.run.run_id, exported_at: new Date().toISOString(), count: events.length, events }, null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `events-${S.dash.run.run_id || 'export'}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('success', '已导出', `${events.length} 条事件`);
}

/* 导出完整运行数据（run + tasks + events）为 JSON */
function exportRun() {
  const run = S.dash.run; if (!run) { toast('warning', '无运行', '当前没有可导出的运行'); return; }
  const data = {
    exported_at: new Date().toISOString(),
    run: run,
    tasks: S.dash.tasks || [],
    agents: S.dash.agents || {},
    gates: S.dash.gates || [],
    events: S.dash.events || [],
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `run-${run.run_id || 'export'}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('success', '已导出', `运行 ${run.run_id} 完整数据`);
}

function summarizeEvent(e) {
  const d = e.data || {};
  if (e.event === 'task_started') return `${d.task_id || ''} 开始执行（第 ${d.attempt || 1} 次）`;
  if (e.event === 'task_succeeded') return `${d.task_id || ''} 完成`;
  if (e.event === 'task_failed') return `${d.task_id || ''} 失败：${d.error || ''}`;
  if (e.event === 'gate_result') return `${d.layer || ''} ${d.verdict || ''}`;
  if (e.event === 'run_started') return `目标：${d.goal || ''}`;
  if (e.event === 'run_finished') return `状态：${d.status || ''}`;
  if (e.event === 'llm_call') return `${d.role || ''} · ${d.model || ''} · ${d.tokens || 0} tok`;
  if (e.event === 'llm_failover') return `${d.from_alias || ''} → ${d.to_alias || ''}`;
  if (e.event === 'budget_exhausted') return `已用 $${d.used || 0} / 上限 $${d.limit || 0}`;
  return d.summary || Object.keys(d).join(', ');
}

/* ============================================================
   计划审查页面（任务编辑 + 批准流程）
   ============================================================ */
function renderPlanPage(container) {
  const run = S.dash.run;
  const tasks = S.dash.tasks || [];
  const isReal = !!S.realMode;
  S.planDirty = S.planDirty || false;

  const renderList = () => {
    list.innerHTML = '';
    const sorted = [...tasks].sort((a, b) => a.id.localeCompare(b.id));
    for (const t of sorted) {
      const meta = STATUS_META[t.status] || STATUS_META.unknown;
      const rm = roleMeta(t.role);
      const row = h('div', { class: 'plan-task-row' },
        h('div', { class: 'plan-task-head' },
          h('span', { class: 'plan-task-id' }, t.id),
          h('span', { class: `status-chip status-${t.status}` }, meta.label),
          h('span', { class: 'plan-task-role', style: `color:${rm.color}` }, rm.full),
          h('div', { style: 'margin-left:auto;display:flex;gap:4px' },
            !isReal ? h('button', { class: 'btn btn-sm btn-danger', onclick: () => {
                if (tasks.some(x => (x.dependencies || []).includes(t.id))) { toast('error', '无法删除', `${t.id} 仍被其他任务依赖`); return; }
                const idx = tasks.indexOf(t); tasks.splice(idx, 1); S.planDirty = true; renderList(); renderStats();
              } }, '删除') : null)),
        h('div', { class: 'plan-task-body' },
          h('div', { class: 'plan-field' },
            h('label', {}, '标题'),
            h('input', { class: 'plan-input', value: t.title || '', disabled: isReal,
              oninput: (e) => { t.title = e.target.value; S.planDirty = true; } })),
          h('div', { class: 'plan-field' },
            h('label', {}, '描述'),
            h('textarea', { class: 'plan-textarea', rows: 2, disabled: isReal,
              oninput: (e) => { t.description = e.target.value; S.planDirty = true; } }, t.description || t.brief || '')),
          h('div', { class: 'plan-field-row' },
            h('div', { class: 'plan-field' },
              h('label', {}, '负责 Agent'),
              h('select', { class: 'plan-select', disabled: isReal,
                onchange: (e) => { t.role = e.target.value; S.planDirty = true; } },
                ...ROLE_ORDER.map(r => h('option', { value: r, selected: t.role === r }, roleMeta(r).full)))),
            h('div', { class: 'plan-field' },
              h('label', {}, '优先级'),
              h('select', { class: 'plan-select', disabled: isReal,
                onchange: (e) => { t.priority = parseInt(e.target.value); S.planDirty = true; } },
                ...[1, 3, 5, 7, 9].map(p => h('option', { value: p, selected: (t.priority || 5) === p }, `P${p}`)))),
            h('div', { class: 'plan-field' },
              h('label', {}, '超时 (秒)'),
              h('input', { class: 'plan-input', type: 'number', value: t.timeout_seconds || 300, disabled: isReal,
                oninput: (e) => { t.timeout_seconds = parseInt(e.target.value) || 300; S.planDirty = true; } })),
            h('div', { class: 'plan-field' },
              h('label', {}, '依赖'),
              h('input', { class: 'plan-input', value: (t.dependencies || []).join(', '), disabled: isReal,
                placeholder: 'T-001, T-002',
                onchange: (e) => {
                  const deps = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
                  const invalid = deps.filter(d => !tasks.some(x => x.id === d));
                  if (invalid.length) { toast('error', '无效依赖', invalid.join(', ') + ' 不存在'); return; }
                  if (deps.includes(t.id)) { toast('error', '循环依赖', '不能依赖自身'); return; }
                  t.dependencies = deps; S.planDirty = true;
                } })))));
      list.appendChild(row);
    }
  };

  const renderStats = () => {
    const totalTasks = tasks.length;
    const roles = new Set(tasks.map(t => t.role));
    const estCost = tasks.reduce((s, t) => s + ((t.result && t.result.cost_usd) || 0.02), 0);
    const estTime = tasks.reduce((s, t) => s + ((t.result && t.result.duration_ms) || 60000), 0);
    stats.innerHTML = '';
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, String(totalTasks)), h('div', { class: 'plan-stat-label' }, '任务数')));
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, String(roles.size)), h('div', { class: 'plan-stat-label' }, '涉及角色')));
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, `$${estCost.toFixed(3)}`), h('div', { class: 'plan-stat-label' }, '估算费用')));
    stats.appendChild(h('div', { class: 'plan-stat' }, h('div', { class: 'plan-stat-val' }, fmtDur(estTime)), h('div', { class: 'plan-stat-label' }, '估算耗时')));
  };

  // 页面头部
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '计划审查'),
      h('div', { class: 'plan-goal', title: run.project_goal }, simplifyGoal(run.project_goal || '未命名项目'))),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      !isReal ? h('button', { class: 'btn', onclick: () => {
          const nextId = 'T-' + String(tasks.length + 1).padStart(3, '0');
          tasks.push({ id: nextId, title: '新任务', description: '', role: 'backend_dev', status: 'pending', priority: 5, timeout_seconds: 300, dependencies: [] });
          S.planDirty = true; renderList(); renderStats();
        } }, '+ 添加任务') : null,
      !isReal ? h('button', { class: 'btn', onclick: () => { S.planDirty = false; toast('success', '已保存', '计划已保存到内存'); } }, '保存计划') : null,
      !isReal ? h('button', { class: 'btn btn-primary', onclick: () => {
          if (!tasks.length) { toast('error', '无法批准', '任务 DAG 为空'); return; }
          const noRole = tasks.filter(t => !t.role);
          if (noRole.length) { toast('error', '无法批准', noRole.map(t => t.id).join(', ') + ' 缺少负责 Agent'); return; }
          confirmAction('批准计划', `共 ${tasks.length} 个任务，涉及 ${new Set(tasks.map(t=>t.role)).size} 个角色。批准后将进入执行阶段。`, () => {
            run.status = 'running'; run.phase = 'execution';
            tasks.forEach(t => { if (t.status === 'pending') t.status = 'ready'; });
            pushEvent('human_approved', { note: 'plan approved', task_count: tasks.length });
            S.planDirty = false; renderDashboard(); toast('success', '计划已批准', '进入执行阶段'); switchNav('dashboard');
          });
        } }, '批准并执行') : null,
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  // 只读提示
  if (isReal) {
    container.appendChild(h('div', { class: 'plan-readonly-banner' },
      '真实模式下计划为只读。如需修改，请通过 CLI 或 API 编辑计划后重新加载。'));
  }

  // 统计栏
  const stats = h('div', { class: 'plan-stats' });
  container.appendChild(stats);
  renderStats();

  // 任务列表
  const list = h('div', { class: 'plan-task-list' });
  container.appendChild(list);
  renderList();
}

const ROLE_ORDER = ['project_manager','product_manager','architect','backend_dev','frontend_dev','qa_engineer','devops_engineer','technical_writer'];

/* 常用模型别名 */
const COMMON_MODELS = ['gpt-4o','gpt-4o-mini','gpt-4-turbo','claude-3-5-sonnet','claude-3-opus','deepseek-chat','deepseek-coder','qwen-max','qwen-plus','glm-4','gemini-pro','mock'];

/* 模型切换弹窗 */
function openModelSwitcher() {
  const current = S.dash.run.model || (S.settings && S.settings.defaultModel) || 'gpt-4o';
  showModal(h('div', {},
    h('h3', { class: 'modal-title' }, '切换模型'),
    h('div', { class: 'confirm-text', style: 'color:#6E6E73;margin-bottom:12px' }, `当前模型：${current}`),
    h('div', { style: 'display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:14px' },
      ...COMMON_MODELS.map(m => h('button', {
        class: 'btn' + (m === current ? ' btn-primary' : ''),
        style: 'font-size:11px;padding:6px 4px',
        onclick: () => { applyModel(m); closeModal(); }
      }, m))),
    h('div', { style: 'display:flex;gap:8px;align-items:center;margin-bottom:14px' },
      h('input', { id: 'model-custom', type: 'text', placeholder: '自定义别名…',
        style: 'flex:1;padding:6px 10px;border:1px solid rgba(60,60,67,.18);border-radius:6px;font-size:12px;font-family:inherit' }),
      h('button', { class: 'btn btn-primary', onclick: () => { const v = $('#model-custom').value.trim(); if (v) { applyModel(v); closeModal(); } } }, '应用')),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', onclick: closeModal }, '取消'))));
}

async function applyModel(alias) {
  if (S.realMode && S.dash.run && S.dash.run.dir) {
    try {
      await window.studio.switchModel(S.dash.run.dir, alias);
      S.dash.run.model = alias;
      if (S.settings) { S.settings.defaultModel = alias; window.studio.setSettings(S.settings).catch(() => {}); }
      pushEvent('llm_model_switched', { from: S.dash.run.model, to: alias });
      renderChrome(); renderDashboard();
      toast('success', '模型已切换', alias);
    } catch (e) {
      toast('error', '切换失败', String(e.message || e));
    }
  } else {
    S.dash.run.model = alias;
    if (S.settings) { S.settings.defaultModel = alias; window.studio.setSettings(S.settings).catch(() => {}); }
    pushEvent('llm_model_switched', { from: S.dash.run.model, to: alias });
    renderChrome(); renderDashboard();
    toast('success', '模型已切换', alias);
  }
}

/* ============================================================
   任务图全屏页（分层列表视图）
   ============================================================ */
function renderDagPage(container) {
  S.taskFilter = S.taskFilter || { q: '', status: 'all' };
  const renderList = () => {
    wrap.innerHTML = '';
    const allTasks = S.dash.tasks || [];
    const q = (S.taskFilter.q || '').toLowerCase();
    const filtered = allTasks.filter(t => {
      if (S.taskFilter.status !== 'all' && t.status !== S.taskFilter.status) return false;
      if (!q) return true;
      return (t.id || '').toLowerCase().includes(q) ||
             (t.title || '').toLowerCase().includes(q) ||
             (t.role || '').toLowerCase().includes(q) ||
             (t.description || '').toLowerCase().includes(q);
    });
    if (!filtered.length) { wrap.appendChild(h('div', { class: 'ph-empty' }, '无匹配任务')); return; }
    const { layers } = computeLayers(filtered);
    layers.forEach((ids, li) => {
      const layerTasks = ids.map(id => filtered.find(t => t.id === id)).filter(Boolean);
      const layerEl = h('div', { class: 'dag-layer' },
        h('div', { class: 'dag-layer-head' }, `第 ${li + 1} 层 · ${layerTasks.length} 个任务`));
      const listEl = h('div', { class: 'dag-layer-tasks' });
      for (const t of layerTasks) {
        const meta = ROLE_META[t.role] || { label: t.role, color: '#8E8E93' };
        listEl.appendChild(h('div', { class: 'dag-task-row', onclick: () => openTaskDetail(t.id) },
          h('span', { class: `ts-dot ts-${t.status}` }),
          h('span', { class: 'dag-task-id' }, t.id),
          h('span', { class: 'dag-task-title' }, trunc(t.title, 40)),
          h('span', { class: 'dag-task-role', style: `color:${meta.color}` }, meta.label || t.role),
          h('span', { class: 'dag-task-deps' }, (t.dependencies || []).length ? `← ${t.dependencies.join(', ')}` : '—'),
          h('span', { class: `dag-task-status status-${t.status}` }, t.status)));
      }
      layerEl.appendChild(listEl);
      wrap.appendChild(layerEl);
    });
  };

  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '任务依赖图'),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      h('input', { type: 'text', placeholder: '搜索任务…', value: S.taskFilter.q,
        style: 'width:180px;padding:6px 10px;border:1px solid rgba(60,60,67,.18);border-radius:6px;font-size:12px;font-family:inherit',
        oninput: (e) => { S.taskFilter.q = e.target.value; renderList(); } }),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  // 状态筛选 chips
  const statuses = ['all', 'pending', 'ready', 'running', 'blocked', 'review', 'succeeded', 'failed', 'skipped'];
  const statusLabels = { all: '全部', pending: '待处理', ready: '就绪', running: '运行中', blocked: '阻塞', review: '评审中', succeeded: '已完成', failed: '失败', skipped: '已跳过' };
  const filterBar = h('div', { class: 'ev-filter-bar', style: 'margin-bottom:12px' });
  statuses.forEach(st => {
    filterBar.appendChild(h('button', { class: 'ev-filter-btn' + (S.taskFilter.status === st ? ' active' : ''),
      onclick: () => { S.taskFilter.status = st; $$('#placeholder-view .ev-filter-btn').forEach(b => b.classList.remove('active')); filterBar.querySelectorAll('.ev-filter-btn').forEach(b => b.classList.remove('active')); event.target.classList.add('active'); renderList(); } },
      statusLabels[st] || st));
  });
  container.appendChild(filterBar);

  const wrap = h('div', { class: 'dag-full-wrap' });
  container.appendChild(wrap);
  renderList();
}

/* ============================================================
   Agents 全屏页
   ============================================================ */
function renderAgentsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, 'Agents'),
    h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回')));
  const grid = h('div', { class: 'agents-full-grid' });
  container.appendChild(grid);
  const agents = S.dash.agents || {};
  for (const [role, a] of Object.entries(agents)) {
    const meta = ROLE_META[role] || { label: role, color: '#8E8E93' };
    const myTasks = S.dash.tasks.filter(t => t.role === role);
    // 派生统计
    const totalCost = myTasks.reduce((s, t) => s + ((t.result && t.result.cost_usd) || 0), 0);
    const totalRetries = myTasks.reduce((s, t) => s + Math.max(0, (t.attempts || 1) - 1), 0);
    const durations = myTasks.filter(t => t.result && t.result.duration_ms).map(t => t.result.duration_ms);
    const avgDur = durations.length ? durations.reduce((s, v) => s + v, 0) / durations.length : 0;
    const total = myTasks.length || 1;
    const successRate = Math.round((a.done || 0) / total * 100);
    grid.appendChild(h('div', { class: 'agent-full-card' },
      h('div', { class: 'agent-full-head' },
        h('span', { class: 'agent-full-dot', style: `background:${meta.color}` }),
        h('span', { class: 'agent-full-name' }, meta.label || role),
        h('span', { class: `agent-full-status status-${a.status}` }, a.status)),
      h('div', { class: 'agent-full-body' },
        h('div', { class: 'agent-full-row' }, h('span', { class: 'k' }, '当前任务'), h('span', { class: 'v' }, a.task || '—')),
        h('div', { class: 'agent-full-row' }, h('span', { class: 'k' }, '最近动作'), h('span', { class: 'v' }, a.action || '—')),
        h('div', { class: 'agent-full-stats' },
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${a.done || 0}`), h('div', { class: 'af-stat-label' }, '已完成')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${a.fail || 0}`), h('div', { class: 'af-stat-label' }, '失败')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${successRate}%`), h('div', { class: 'af-stat-label' }, '成功率')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${totalRetries}`), h('div', { class: 'af-stat-label' }, '重试')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, fmtTokens(a.tokens || 0)), h('div', { class: 'af-stat-label' }, 'Token')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, totalCost ? `$${totalCost.toFixed(3)}` : '—'), h('div', { class: 'af-stat-label' }, '费用')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, avgDur ? fmtDur(avgDur) : '—'), h('div', { class: 'af-stat-label' }, '平均耗时')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, a.model || '—'), h('div', { class: 'af-stat-label' }, '模型'))),
        h('div', { class: 'agent-full-tasks' },
          h('div', { class: 'agent-full-tasks-title' }, `任务列表（${myTasks.length}）`),
          ...myTasks.map(t => h('div', { class: 'agent-full-task-item',
            onclick: () => { switchNav('dashboard'); setTimeout(() => openTaskDetail(t.id), 100); } },
            h('span', { class: `ts-dot ts-${t.status}` }),
            h('span', { class: 'agent-full-task-id' }, t.id),
            h('span', { class: 'agent-full-task-title' }, trunc(t.title, 30))))))));
  }
}

/* ============================================================
   门禁全屏页
   ============================================================ */
function renderGatesPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '质量门禁'),
    h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回')));
  const list = h('div', { class: 'gates-full-list' });
  container.appendChild(list);
  const gates = S.dash.gates || [];
  for (const g of gates) {
    const reviews = S.dash.tasks.flatMap(t => (t.reviews || []).filter(r => r.layer === g.layer));
    list.appendChild(h('div', { class: 'gate-full-card' },
      h('div', { class: 'gate-full-head' },
        h('span', { class: 'gate-full-id' }, g.id),
        h('span', { class: 'gate-full-name' }, g.name),
        h('span', { class: `gate-full-status status-${g.status}` }, g.status)),
      h('div', { class: 'gate-full-stats' },
        h('div', { class: 'gate-full-stat' }, h('div', { class: 'n' }, g.checks.total), h('div', { class: 'l' }, '检查项')),
        h('div', { class: 'gate-full-stat ok' }, h('div', { class: 'n' }, g.checks.passed), h('div', { class: 'l' }, '通过')),
        h('div', { class: 'gate-full-stat err' }, h('div', { class: 'n' }, g.checks.failed), h('div', { class: 'l' }, '失败'))),
      h('div', { class: 'gate-full-verdict' }, g.verdict || ''),
      reviews.length ? h('div', { class: 'gate-full-reviews' },
        h('div', { class: 'gate-full-reviews-title' }, `评审记录（${reviews.length}）`),
        ...reviews.slice(0, 10).map(r => h('div', { class: 'gate-review-item' },
          h('span', { class: `gate-review-verdict ${r.verdict}` }, r.verdict),
          h('span', { class: 'gate-review-task' }, r.task_id || ''),
          h('span', { class: 'gate-review-text' }, trunc(r.summary || r.issues?.join('; ') || '', 80))))) : null,
      !S.realMode ? h('div', { class: 'gate-full-actions' },
        h('button', { class: 'btn btn-sm btn-primary', onclick: () => { acceptGate(g); renderGatesPage(container.parentElement); } }, '批准通过'),
        h('button', { class: 'btn btn-sm btn-danger', onclick: () => { rejectGate(g); renderGatesPage(container.parentElement); } }, '驳回返工'),
        h('button', { class: 'btn btn-sm', onclick: () => { g.status = 'running'; g.checks = { total: g.checks.total, passed: 0, failed: 0 }; toast('info', '重试门禁', g.name + ' 重新运行'); renderGatesPage(container.parentElement); } }, '重试'))
        : h('div', { class: 'gate-full-readonly' }, '真实模式下门禁审批需通过 CLI 或 API 执行')));
  }
}

function switchNav(key){
  S.nav=key; persistState();
  $$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.nav===key));
  const dash=$('#dashboard-view'), ph=$('#placeholder-view');
  if(key==='dashboard'){ ph.classList.add('hidden'); dash.classList.remove('hidden'); return; }
  dash.classList.add('hidden'); ph.classList.remove('hidden');
  ph.innerHTML='';
  if(key==='artifacts'){ renderArtifactsPage(ph); return; }
  if(key==='settings'){ renderSettingsPage(ph); return; }
  if(key==='events'){ renderEventsPage(ph); return; }
  if(key==='dag'){ renderDagPage(ph); return; }
  if(key==='tasks'){ renderPlanPage(ph); return; }
  if(key==='agents'){ renderAgentsPage(ph); return; }
  if(key==='gates'){ renderGatesPage(ph); return; }
  const p=PLACEHOLDERS[key]||{title:key,desc:'该页面开发中。'};
  ph.appendChild(h('div',{class:'ph-icon'},h('svg',{width:30,height:30,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':1.6,'stroke-linecap':'round','stroke-linejoin':'round'},h('rect',{x:'3',y:'3',width:'18',height:'18',rx:'3'}),h('path',{d:'M8 12h8'}))));
  ph.appendChild(h('h2',{},p.title)); ph.appendChild(h('p',{},p.desc));
  ph.appendChild(h('button',{class:'btn btn-primary',onclick:()=>switchNav('dashboard')},'返回运行总览'));
}

/* ============================================================
   Dashboard 主渲染 & 顶栏同步
   ============================================================ */
function renderDashboard(){
  if(!S.dash)return;
  renderRunStatusBar(); renderDAG(); renderDAGFallback();
  renderAgents(); renderGates(); renderCostPanel(); renderEvents(); renderChrome();
}
function renderChrome(){
  const d=S.dash; if(!d)return; const run=d.run; const st=run.stats||{};
  const meta=RUN_META[run.status]||RUN_META.idle;
  $('#tb-run-dot').className='dot '+meta.dot; $('#tb-run-label').textContent=meta.label;
  $('#tb-workspace').textContent=simplifyGoal(run.project_goal);
  const cost=st.estimated_cost_usd||0, pct=Math.min(100,cost/run.budget_limit_usd*100);
  $('#tb-budget-tokens').textContent=fmtTokens((st.prompt_tokens||0)+(st.completion_tokens||0));
  $('#tb-budget-cost').textContent=fmtCost(cost);
  $('#tb-budget-pct').textContent=Math.round(pct)+'%';
  $('#tb-budget-fill').style.width=pct+'%';
  const tb=$('#tb-budget'); tb.classList.toggle('warn',pct>=90&&pct<100); tb.classList.toggle('err',pct>=100);
  $('#sb-provider').textContent=run.provider; $('#sb-model').textContent=run.model;
  $('#sb-budget-pct').textContent=Math.round(pct)+'%'; $('#sb-budget-fill').style.width=pct+'%';
  const sbf=document.querySelector('.sb-progress'); sbf.classList.toggle('warn',pct>=90&&pct<100); sbf.classList.toggle('err',pct>=100);
  $('#sb-conn-dot').className='dot ' + (S.realMode ? 'running' : 'idle');
  $('#sb-conn-label').textContent = S.realMode ? '已连接' : '演示模式';
  const ds = $('#sb-datasource'); if (ds) { ds.textContent = S.realMode ? '真实' : '演示'; ds.style.color = S.realMode ? '#34C759' : '#FF9F0A'; }
}

/* ============================================================
   演示边界状态
   ============================================================ */
function applyDemoState(state){
  stopSim();
  if(state==='empty'){
    // 无运行记录：清空数据层，在状态条显示引导；重置按钮直接重载页面恢复骨架
    S.dash=null; stopSim();
    $('#run-status-bar').innerHTML='';
    $('#run-status-bar').appendChild(h('div',{class:'banner info',style:'margin:0'},
      '没有运行记录。点击右上角「新建运行」开始第一次多智能体协作。'));
    $('#run-status-bar').appendChild(h('button',{class:'btn btn-primary',style:'margin-top:8px',onclick:()=>location.reload()},'创建示例运行'));
    renderChrome();
    return;
  }
  initMockData(); const d=S.dash; const run=d.run;
  if(state==='planning'){ run.status='planning'; run.phase='planning'; for(const t of d.tasks)t.status='pending'; d.gates.forEach(g=>g.status='pending'); }
  if(state==='budget_warn'){ run.stats.estimated_cost_usd=0.92; toast('warning','预算预警','已使用 92%'); }
  if(state==='budget_exhausted'){ run.status='running'; run.stats.estimated_cost_usd=1.05; pushEvent('budget_exhausted',{}); toast('error','预算耗尽','运行将优雅收尾'); }
  if(state==='paused'){ run.status='paused'; }
  if(state==='failed'){ run.status='failed'; const t=d.tasks.find(x=>x.id==='T-11'); t.status='failed'; t.error='集成测试 12 项断言失败'; pushEvent('error',{where:'T-11',message:'断言失败'},'T-11','backend_dev'); }
  if(state==='gates_failed'){ const g=d.gates[1]; g.status='failed'; g.checks={total:8,passed:5,failed:3};
    g.fail_reason='关键代码评审发现鉴权中间件漏洞'; g.impact='影响交付物 T-11、T-14'; g.suggestion='修复鉴权后重新提交'; g.return_task='T-07'; g.extra_cost='$0.08';
    pushEvent('gate_result',{layer:'llm',verdict:'reject',gate:'G2'}); }
  if(state==='completed'){ run.status='completed'; run.phase='delivery'; for(const t of d.tasks)t.status='succeeded'; d.gates.forEach(g=>{g.status='passed';g.checks={total:g.checks.total,passed:g.checks.total,failed:0};}); }
  renderDashboard();
  if(state==='running') startSim();
}

/* ============================================================
   Boot
   ============================================================ */
/* ============================================================
   真实数据接入：从 IPC 加载 run.json/tasks.json/ledger.jsonl
   ============================================================ */

/* 将真实 task（tasks.json）映射为渲染层期望的格式 */
function mapRealTask(t) {
  const status = t.status || 'pending';
  return Object.assign({}, t, {
    progress: status === 'running' ? (t.progress || 50) : 0,
    error: t.error || (status === 'failed' ? (t.result && t.result.error) || '执行失败' : null),
    brief: t.brief || t.description || '',
    result: t.result || null,
    reviews: t.reviews || [],
  });
}

/* 从任务列表派生 8 个 Agent 的状态 */
function deriveAgents(tasks) {
  const roles = ['project_manager','product_manager','architect','backend_dev','frontend_dev','qa_engineer','devops_engineer','technical_writer'];
  const agents = {};
  for (const r of roles) {
    const mine = tasks.filter(t => t.role === r);
    const done = mine.filter(t => t.status === 'succeeded').length;
    const fail = mine.filter(t => t.status === 'failed').length;
    const running = mine.find(t => ['running','review','rework'].includes(t.status));
    const blocked = mine.find(t => t.status === 'blocked');
    let status = 'queued';
    if (running) status = 'executing';
    else if (fail > 0) status = 'error';
    else if (mine.length > 0 && done === mine.length) status = 'done';
    else if (blocked) status = 'queued';
    const tokens = mine.reduce((s,t) => s + ((t.result && t.result.tokens_used) || 0), 0);
    agents[r] = {
      status,
      task: running ? running.title : (done > 0 ? '空闲' : '等待中'),
      action: running ? (running.brief || '执行中') : (done > 0 ? `已完成 ${done} 项` : '等待依赖'),
      done, fail,
      model: '—',
      tokens, cost: 0,
      updated: mine.length ? (mine[mine.length-1].updated_at || nowTs(0)) : nowTs(0),
    };
  }
  return agents;
}

/* 从任务 reviews 派生三层门禁状态 */
function deriveGates(tasks) {
  const allReviews = tasks.flatMap(t => t.reviews || []);
  const byLayer = (layer) => allReviews.filter(r => r.layer === layer);
  const summarize = (reviews) => {
    const total = reviews.length;
    const passed = reviews.filter(r => r.verdict === 'approve').length;
    const failed = reviews.filter(r => r.verdict === 'reject').length;
    return { total, passed, failed };
  };
  const g1 = summarize(byLayer('static'));
  const g2 = summarize(byLayer('llm'));
  const finalTask = tasks.find(t => t.id === 'T-FINAL');
  const g3Done = finalTask && finalTask.status === 'succeeded';
  const g3Status = g3Done ? 'passed' : (tasks.some(t => ['running','review'].includes(t.status)) ? 'running' : 'pending');
  return [
    { id:'G1', layer:'static', name:'静态校验',
      status: g1.total ? (g1.failed ? 'failed' : 'passed') : 'running',
      checks: g1, duration_ms: null,
      verdict: g1.total ? `${g1.passed}/${g1.total} 项通过` : '等待任务执行后触发',
      rework: true, need_human: false, task_ref: tasks.filter(t=>t.status==='failed').map(t=>t.id).join(', ') || '—' },
    { id:'G2', layer:'llm', name:'LLM 评审',
      status: g2.total ? (g2.failed ? 'failed' : 'passed') : 'pending',
      checks: g2, duration_ms: null,
      verdict: g2.total ? `${g2.passed}/${g2.total} 项通过` : '等待静态校验通过后触发',
      rework: true, need_human: true, task_ref: '—' },
    { id:'G3', layer:'delivery', name:'交付确认',
      status: g3Status, checks: { total:1, passed: g3Done?1:0, failed:0 }, duration_ms: null,
      verdict: g3Done ? 'T-FINAL 已交付，等待人工确认' : '等待全部任务完成',
      rework: false, need_human: true, task_ref: 'T-FINAL' },
  ];
}

/* 从 run.json 派生渲染层需要的字段 */
function enrichRun(run) {
  if (!run) return run;
  const cfg = run.config_snapshot || {};
  const stats = run.stats || {};
  // 派生 phase
  let phase = 'planning';
  if (stats.succeeded > 0 || stats.failed > 0) phase = 'execution';
  if (run.status === 'completed' || run.status === 'failed') phase = 'delivery';
  return Object.assign({}, run, {
    phase,
    model: cfg.default_model || '—',
    budget_limit_usd: cfg.max_cost_usd || 50,
  });
}

/* 加载真实 run 数据并启动 watch */
async function loadRealRun(dir) {
  try {
    const snap = await window.studio.loadRun(dir);
    if (!snap || !snap.run) return false;
    const tasks = (snap.tasks || []).map(mapRealTask);
    // 加载 ledger 历史
    let events = [];
    try {
      const h = await window.studio.ledgerHistory(dir, 300);
      events = (h && h.events) || [];
    } catch {}
    S.dash = {
      run: Object.assign(enrichRun(snap.run), { dir }),
      tasks,
      agents: deriveAgents(tasks),
      gates: deriveGates(tasks),
      events,
    };
    S.realMode = true;
    S.watchDir = dir;
    // 启动实时 watch
    try { await window.studio.watchRun(dir, true); } catch {}
    return true;
  } catch (e) {
    console.error('loadRealRun failed', e);
    return false;
  }
}

/* watch 事件：snapshot 更新（run.json/tasks.json 变化） */
function onWatchSnapshot(payload) {
  if (!payload || payload.dir !== S.watchDir) return;
  if (payload.run) S.dash.run = Object.assign(enrichRun(payload.run), { dir: S.watchDir });
  if (payload.tasks) {
    S.dash.tasks = payload.tasks.map(mapRealTask);
    S.dash.agents = deriveAgents(S.dash.tasks);
    S.dash.gates = deriveGates(S.dash.tasks);
  }
  renderDashboard();
}

/* watch 事件：ledger 增量事件 */
function onWatchLedger(payload) {
  if (!payload || payload.dir !== S.watchDir) return;
  const newEvents = payload.events || [];
  if (newEvents.length) {
    S.dash.events.push(...newEvents);
    if (S.dash.events.length > 500) S.dash.events = S.dash.events.slice(-500);
    renderEvents();
  }
}

/* ============================================================
   运行列表与切换
   ============================================================ */
const RUN_STATUS_COLOR = {
  running: '#007AFF', completed: '#34C759', failed: '#FF3B30',
  paused: '#FF9F0A', cancelled: '#8E8E93', planning: '#5AC8FA',
  preparing: '#8E8E93', awaiting_approval: '#AF52DE',
};

async function loadRunList() {
  const listEl = $('#run-dropdown-list');
  if (!listEl) return;
  try {
    const runs = await window.studio.listRuns();
    if (!runs || !runs.length) {
      listEl.innerHTML = '<div class="rd-empty">暂无运行记录</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const r of runs.slice(0, 20)) {
      const color = RUN_STATUS_COLOR[r.status] || '#8E8E93';
      const isActive = S.dash.run && S.dash.run.dir === r.dir;
      const canDelete = r.status !== 'running';
      const item = h('div', { class: 'rd-item' + (isActive ? ' active' : '') },
        h('span', { class: 'rd-dot', style: `background:${color}`, onclick: () => { closeRunDropdown(); switchRun(r.dir); } }),
        h('div', { class: 'rd-item-info', onclick: () => { closeRunDropdown(); switchRun(r.dir); } },
          h('div', { class: 'rd-item-title', title: r.goal || r.runId }, simplifyGoal(r.goal || r.runId || '未命名')),
          h('div', { class: 'rd-item-meta' }, `${r.runId || ''} · ${r.status || 'unknown'} · ${fmtDateTime(r.createdAt || r.updatedAt)}`)),
        h('button', { class: 'rd-del', title: canDelete ? '删除运行' : '运行中无法删除',
          disabled: !canDelete,
          onclick: async (e) => {
            e.stopPropagation();
            if (!confirm(`确认删除运行「${simplifyGoal(r.goal || r.runId)}」？\n此操作不可恢复。`)) return;
            try {
              await window.studio.deleteRun(r.dir);
              toast('success', '已删除', r.runId || '');
              if (isActive) {
                const remaining = await window.studio.listRuns();
                if (remaining && remaining.length) await switchRun(remaining[0].dir);
                else { S.realMode = false; initMockData(); renderDashboard(); }
              }
              loadRunList();
            } catch (err) { toast('error', '删除失败', String(err.message || err)); }
          } },
          h('svg', { width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
            h('polyline', { points: '3 6 5 6 21 6' }),
            h('path', { d: 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2' }))),
        !isActive ? h('button', { class: 'rd-cmp', title: '与当前运行对比',
          onclick: (e) => { e.stopPropagation(); closeRunDropdown(); openCompareModal(r); } },
          h('svg', { width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
            h('path', { d: 'M14 3v4a1 1 0 0 0 1 1h4' }),
            h('path', { d: 'M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z' }),
            h('path', { d: 'M9 13h6M9 17h6' })) : null));
      listEl.appendChild(item);
    }
  } catch (e) {
    listEl.innerHTML = '<div class="rd-empty">加载失败</div>';
  }
}

function toggleRunDropdown() {
  const dd = $('#run-dropdown');
  if (!dd) return;
  if (dd.classList.contains('hidden')) {
    loadRunList();
    dd.classList.remove('hidden');
  } else {
    dd.classList.add('hidden');
  }
}
function closeRunDropdown() {
  const dd = $('#run-dropdown');
  if (dd) dd.classList.add('hidden');
}

async function openCompareModal(otherRun) {
  // 加载另一个 run 的数据
  let otherData = null;
  try {
    const [run, tasks, events] = await Promise.all([
      window.studio.readRun(otherRun.dir),
      window.studio.readTasks(otherRun.dir).catch(() => ({ tasks: [] })),
      window.studio.readLedger(otherRun.dir).catch(() => []),
    ]);
    otherData = { run, tasks: tasks.tasks || [], events };
  } catch (e) {
    toast('error', '加载失败', String(e.message || e));
    return;
  }

  const cur = S.dash.run, curTasks = S.dash.tasks || [];
  const curCost = cur?.stats?.estimated_cost_usd || 0;
  const otherCost = otherData.run?.stats?.estimated_cost_usd || 0;
  const curTokens = (cur?.stats?.prompt_tokens || 0) + (cur?.stats?.completion_tokens || 0);
  const otherTokens = (otherData.run?.stats?.prompt_tokens || 0) + (otherData.run?.stats?.completion_tokens || 0);
  const curSuccess = curTasks.filter(t => t.status === 'passed' || t.status === 'succeeded').length;
  const otherSuccess = otherData.tasks.filter(t => t.status === 'passed' || t.status === 'succeeded').length;
  const curFailed = curTasks.filter(t => t.status === 'failed').length;
  const otherFailed = otherData.tasks.filter(t => t.status === 'failed').length;

  const rows = [
    ['状态', cur?.status || '—', otherData.run?.status || '—'],
    ['任务数', String(curTasks.length), String(otherData.tasks.length)],
    ['成功', String(curSuccess), String(otherSuccess)],
    ['失败', String(curFailed), String(otherFailed)],
    ['费用', `$${curCost.toFixed(4)}`, `$${otherCost.toFixed(4)}`],
    ['Token', fmtTokens(curTokens), fmtTokens(otherTokens)],
    ['LLM 调用', String(cur?.stats?.llm_calls || 0), String(otherData.run?.stats?.llm_calls || 0)],
    ['模型', cur?.model || cur?.config_snapshot?.default_model || '—', otherData.run?.model || otherData.run?.config_snapshot?.default_model || '—'],
    ['创建时间', fmtDateTime(cur?.created_at || cur?.start_time), fmtDateTime(otherData.run?.created_at || otherData.run?.start_time)],
  ];

  const diff = (a, b) => {
    const na = parseFloat(a), nb = parseFloat(b);
    if (isNaN(na) || isNaN(nb)) return '';
    const d = na - nb;
    if (Math.abs(d) < 0.0001) return '<span class="cmp-diff zero">0</span>';
    return `<span class="cmp-diff ${d > 0 ? 'pos' : 'neg'}">${d > 0 ? '+' : ''}${d.toFixed(4)}</span>`;
  };

  openModal('运行对比', h('div', { class: 'cmp-wrap' },
    h('div', { class: 'cmp-headers' },
      h('div', { class: 'cmp-header' }, '指标'),
      h('div', { class: 'cmp-header cur' }, simplifyGoal(cur?.goal || cur?.run_id || '当前')),
      h('div', { class: 'cmp-header' }, simplifyGoal(otherRun.goal || otherRun.runId || '对比')),
      h('div', { class: 'cmp-header' }, '差值')),
    ...rows.map(([label, a, b]) => h('div', { class: 'cmp-row' },
      h('div', { class: 'cmp-label' }, label),
      h('div', { class: 'cmp-val' }, String(a)),
      h('div', { class: 'cmp-val' }, String(b)),
      h('div', { class: 'cmp-diff-cell' }, diff(a, b))))));
}

async function switchRun(dir) {
  // 停止旧 watch
  if (S.watchDir) {
    try { await window.studio.unwatchRun(S.watchDir); } catch {}
  }
  // 显示加载态
  const overlay = h('div', { class: 'loading-overlay', id: 'loading-overlay' },
    h('div', { class: 'loading-spinner' }),
    h('div', { class: 'loading-text' }, '加载运行数据…'));
  document.body.appendChild(overlay);
  try {
    const ok = await loadRealRun(dir);
    if (ok) {
      renderDashboard();
      persistState();
      toast('info', '已切换', simplifyGoal(S.dash.run.project_goal));
    } else {
      toast('error', '切换失败', '无法加载该运行数据');
    }
  } finally {
    overlay.remove();
  }
}

async function boot(){
  $('#btn-new-run').addEventListener('click',openNewRunModal);
  $('#btn-refresh').addEventListener('click', async () => {
    if (S.realMode && S.watchDir) {
      try { await loadRealRun(S.watchDir); renderDashboard(); toast('info', '已刷新', '数据已重新加载'); }
      catch (e) { toast('error', '刷新失败', String(e.message || e)); }
    } else { renderDashboard(); toast('info', '已刷新', '演示数据已重新渲染'); }
  });
  $('#btn-notify').addEventListener('click',()=>{ $('#notify-dot').classList.add('hidden'); toast('info','通知','暂无新通知'); });
  $('#btn-user').addEventListener('click',openUserMenu);
  $('#tb-workspace').addEventListener('click',(e)=>{ e.stopPropagation(); toggleRunDropdown(); });
  document.addEventListener('click',(e)=>{ if(!e.target.closest('#run-dropdown')&&!e.target.closest('#tb-workspace')) closeRunDropdown(); });
  $$('.nav-item').forEach(b=>b.addEventListener('click',()=>switchNav(b.dataset.nav)));
  window.addEventListener('error',(e)=>toast('error','界面错误',e.message));
  window.addEventListener('unhandledrejection',(e)=>toast('error','未处理异常',String(e.reason&&e.reason.message||e.reason)));

  // 键盘快捷键
  window.addEventListener('keydown',(e)=>{
    // Escape：关闭面板/弹窗/下拉
    if(e.key==='Escape'){
      closeDetailPanel(); closeModal(); closeRunDropdown();
      return;
    }
    // Cmd/Ctrl + N：新建运行
    if((e.metaKey||e.ctrlKey)&&e.key==='n'){ e.preventDefault(); openNewRunModal(); return; }
    // 数字键 1-7：切换导航
    if(!e.metaKey&&!e.ctrlKey&&!e.altKey&&/^[1-8]$/.test(e.key)){
      const pages=['dashboard','tasks','dag','agents','gates','artifacts','events','settings'];
      const idx=parseInt(e.key)-1;
      if(pages[idx]){ switchNav(pages[idx]); }
    }
    // 空格：暂停/恢复（仅在 dashboard 且运行中）
    if(e.key===' '&&S.nav==='dashboard'&&S.dash.run){
      const run=S.dash.run;
      if(run.status==='running'){ e.preventDefault(); /* 暂停按钮逻辑 */ }
    }
  });

  try { const r=await window.studio.getSettings(); S.settings=r.settings; S.env=r.env; } catch { S.settings={}; }
  try { await window.studio.detectPython(); } catch {}

  // 注册 watch 事件监听（真实数据模式下实时更新）
  try {
    window.studio.on('watch:snapshot', onWatchSnapshot);
    window.studio.on('watch:ledger', onWatchLedger);
  } catch {}

  // 真实数据优先：有运行则加载完整真实数据，否则 mock
  S.realMode = false;
  try {
    const runs=await window.studio.listRuns();
    if(runs&&runs.length){
      // 优先恢复上次查看的 run，否则用最新的
      const lastDir = localStorage.getItem('agentcrew:lastRunDir');
      const target = lastDir && runs.find(r=>r.dir===lastDir) ? lastDir : runs[0].dir;
      const ok = await loadRealRun(target);
      if (ok) S.realMode = true;
    }
  } catch {}
  if(!S.realMode) initMockData();

  // 恢复 DAG 缩放/平移状态
  try {
    const savedView = localStorage.getItem('agentcrew:dagView');
    if (savedView) { const v = JSON.parse(savedView); S.view = { zoom: v.zoom || 1, x: v.x || 0, y: v.y || 0 }; }
  } catch {}

  renderDashboard();
  // 恢复上次查看的页面
  const lastPage = localStorage.getItem('agentcrew:lastPage');
  switchNav(lastPage || 'dashboard');
  if (!S.realMode) startSim();
  setInterval(renderChrome,1000);
}

/* 状态持久化 */
function persistState() {
  try {
    if (S.watchDir) localStorage.setItem('agentcrew:lastRunDir', S.watchDir);
    if (S.nav) localStorage.setItem('agentcrew:lastPage', S.nav);
  } catch {}
}

function openUserMenu(){
  const states=[['running','正常运行（模拟推进）'],['planning','正在规划'],['budget_warn','预算预警（90%）'],['budget_exhausted','预算耗尽'],
    ['paused','用户暂停'],['failed','运行失败'],['gates_failed','门禁失败'],['completed','运行完成'],['empty','无运行记录']];
  showModal(h('div',{},
    h('h3',{class:'modal-title'},'账户与演示'),
    h('div',{class:'confirm-text',style:'color:#8E8E93'},'工作区：'+(S.settings&&S.settings.outputDir||'默认')),
    h('div',{style:'margin:10px 0;font-weight:600;font-size:13px'},'演示边界状态：'),
    ...states.map(([k,label])=>h('button',{class:'btn',style:'display:flex;width:100%;margin-bottom:6px;justify-content:flex-start',onclick:()=>{closeModal();applyDemoState(k);}},label)),
    h('div',{class:'modal-actions'},h('button',{class:'btn',onclick:closeModal},'关闭'))));
}

function openNewRunModal(){
  showModal(h('div',{},
    h('h3',{class:'modal-title'},'启动新运行'),
    h('div',{class:'confirm-text'},'用一句话描述要交付的目标：'),
    h('textarea',{id:'nr-goal',rows:3,style:'width:100%;border:1px solid rgba(60,60,67,.2);border-radius:6px;padding:8px;font-family:inherit;font-size:13px;resize:vertical',placeholder:'例如：开发一个短链接生成服务，含 REST API、缓存与部署文档'}),
    h('div',{style:'display:flex;gap:16px;margin-top:10px;font-size:12px;color:#6E6E73'},
      h('label',{style:'display:flex;align-items:center;gap:5px;cursor:pointer'},
        h('input',{type:'checkbox',id:'nr-mock',checked:true}),'Mock 模式'),
      h('label',{style:'display:flex;align-items:center;gap:5px;cursor:pointer'},
        h('input',{type:'checkbox',id:'nr-auto',checked:true}),'自动执行'),
      h('label',{style:'display:flex;align-items:center;gap:5px;cursor:pointer'},
        h('input',{type:'checkbox',id:'nr-demo'}),'Demo 目标')),
    h('div',{class:'modal-actions'},
      h('button',{class:'btn',onclick:closeModal},'取消'),
      h('button',{class:'btn btn-primary',onclick:async ()=>{
        const goal=$('#nr-goal').value.trim();
        const useDemo=$('#nr-demo').checked;
        if(!useDemo && !goal){ toast('warning','需要目标','请输入项目目标或勾选 Demo'); return; }
        closeModal();
        try {
          const r = await window.studio.startRun({
            goal: useDemo ? '' : goal,
            demo: useDemo,
            mock: $('#nr-mock').checked,
            auto: $('#nr-auto').checked,
          });
          toast('success','已启动','运行已提交，正在初始化…');
          // 等待 CLI 创建 run 目录后切换
          setTimeout(async ()=>{
            try {
              const runs = await window.studio.listRuns();
              if(runs && runs.length) await switchRun(runs[0].dir);
            } catch {}
          }, 2500);
        } catch(e) {
          toast('error','启动失败', String(e.message||e));
        }
      }},'启动'))));
}

boot();
