/**
 * 真实运行数据适配：把后端 snapshot / ledger 映射成 UI 领域模型（含派生 agents、gates）。
 */
import { h, s } from '../lib/dom.js';
import { S } from '../lib/store.js';

import { nowTs, stopSim } from './mock.js';
import { connectSSE, resetRealtime } from './realtime.js';

/* 快速连点两个 run 时，旧 loadRealRun 的异步续体不能覆盖新 run 的状态。 */
let loadSeq = 0;
/* C-19：当前载入代次。realtime 的 500ms 防抖快照用它校验响应是否仍是最新一次载入。 */
function currentLoadSeq() { return loadSeq; }
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
  // R7：主管复核是第四层（谁派的活谁验收）。只有真的出现过 supervisor 记录
  // 才追加这张卡——默认（开关关闭）时门禁列表仍是三层，既有断言不受影响。
  const g2s = summarize(byLayer('supervisor'));
  const finalTask = tasks.find(t => t.id === 'T-FINAL');
  const g3Done = finalTask && finalTask.status === 'succeeded';
  const g3Status = g3Done ? 'passed' : (tasks.some(t => ['running','review'].includes(t.status)) ? 'running' : 'pending');
  const gates = [
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
  if (g2s.total) {
    gates.splice(2, 0, {
      id:'G2S', layer:'supervisor', name:'主管复核',
      status: g2s.failed ? 'failed' : 'passed',
      checks: g2s, duration_ms: null,
      verdict: `${g2s.passed}/${g2s.total} 项通过（派活的人验收）`,
      rework: true, need_human: false, task_ref: '—',
    });
  }
  return gates;
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

/* 加载真实 run 数据并启动 watch */async function loadRealRun(dir) {
  const my = ++loadSeq;
  // 从「无 run 的演示模式」启动第一个真实运行时，2.5s 的 mock 推演定时器
  // 还在跑，会按演示逻辑改写真实任务状态、伪造事件。进真实模式前必须停掉。
  stopSim();
  // 换 run（或重刷）前先收掉上一条实时通道：否则旧 SSE 仍往 S.dash.events
  // 推事件，500ms 的快照防抖定时器也可能用旧 watchDir 覆盖新数据。
  resetRealtime();
  try {
    const snap = await window.studio.loadRun(dir);
    if (my !== loadSeq) return false;
    if (!snap || !snap.run) return false;
    const tasks = (snap.tasks || []).map(mapRealTask);
    // 加载 ledger 历史
    let events = [];
    try {
      const h = await window.studio.ledgerHistory(dir, 2000);
      if (my !== loadSeq) return false;
      events = (h && h.events) || [];
    } catch {}
    // 加载流式正文回溯（streams/<task_id>.md，刷新后可恢复 Agent 完整回复正文）。
    // role 从任务派生（正文落盘不含 role），换 run 时一并清空。
    S.convStreams = null;
    try {
      const streams = await window.studio.loadStreams(dir);
      if (my !== loadSeq) return false;
      if (streams && typeof streams === 'object') {
        const roleOf = {};
        tasks.forEach((t) => { roleOf[t.id] = t.role; });
        const conv = {};
        Object.entries(streams).forEach(([tid, text]) => {
          conv[tid] = { role: roleOf[tid] || '', text: String(text || '') };
        });
        if (Object.keys(conv).length) S.convStreams = conv;
      }
    } catch {}
    S.dash = {
      run: Object.assign(enrichRun(snap.run), { dir }),
      tasks,
      agents: deriveAgents(tasks),
      gates: deriveGates(tasks),
      events,
    };
    S._eventKeys = null;    // 换 run：重建事件去重索引
    S.realMode = true;
    S.watchDir = dir;
    // P3：优先 SSE，回退文件 watch。
    // 渲染进程是纯 ESM 且 nodeIntegration=false，没有 Node 的 path 模块；
    // 旧的 path.basename(dir) 在 run.json 缺 run_id 时会抛 ReferenceError，
    // 被下面的 catch 吞掉后表现为「切换失败」。用纯字符串取目录名兜底。
    const fallbackId = String(dir).split(/[\\/]/).filter(Boolean).pop() || String(dir);
    const runId = snap.run.run_id || fallbackId;
    // connectSSE 现在是异步的：只有主进程真的拿到 200 响应头才算连上，
    // 401 / 连接失败一律回退文件 watch（旧实现恒返回 true，实时刷新会全静止）。
    if (!(await connectSSE(runId))) {
      if (my !== loadSeq) return false;
      try { await window.studio.watchRun(dir, true); } catch {}
    }
    return my === loadSeq;
  } catch (e) {
    console.error('loadRealRun failed', e);
    return false;
  }
}

/* watch 事件：snapshot 更新（run.json/tasks.json 变化） */

export {
  currentLoadSeq,
  deriveAgents,
  deriveGates,
  enrichRun,
  loadRealRun,
  mapRealTask,
};
