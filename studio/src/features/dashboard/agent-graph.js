/**
 * Agent 图的数据模型：把任务 DAG 折叠成「执行者 DAG」。
 *
 * 折叠规则：
 *  - 同一 Agent 内部的任务先后顺序不产生边（那不是「谁等谁」，是「先后干」）；
 *  - 跨 Agent 的依赖按 (上游 role → 下游 role) 聚合成一条边，weight = 任务数；
 *  - Agent 之间会互相返工（架构↔后端），先 DFS 标回边、再在无环部分做最长路径分层；
 *    回边不丢，画成卡片下方的虚线回路。
 */
import { AGENT_GAP_X, AGENT_GAP_Y, AH, AW } from './dag-canvas.js';

const ACTIVE_STATES = ['running', 'review', 'rework'];

function agentTone(n) {
  if (n.active) return 'running';
  if (n.failed) return 'failed';
  if (n.blocked) return 'blocked';
  if (n.total && n.done === n.total) return 'succeeded';
  if (n.rest) return 'ready';
  return 'pending';
}

function buildAgentGraph(tasks) {
  const byTask = new Map(tasks.map(t => [t.id, t]));
  const byRole = new Map();
  const nodes = [];
  for (const t of tasks) {
    const role = t.role || 'unknown';
    let n = byRole.get(role);
    if (!n) {
      n = { role, tasks: [], total: 0, done: 0, active: 0, failed: 0, blocked: 0, rest: 0 };
      byRole.set(role, n);
      nodes.push(n);
    }
    n.tasks.push(t); n.total += 1;
    if (t.status === 'succeeded') n.done += 1;
    else if (ACTIVE_STATES.includes(t.status)) n.active += 1;
    else if (t.status === 'failed') n.failed += 1;
    else if (t.status === 'blocked') n.blocked += 1;
    else n.rest += 1;
  }

  const edgeMap = new Map();
  const edges = [];
  for (const t of tasks) {
    const to = t.role || 'unknown';
    for (const depId of (t.dependencies || [])) {
      const up = byTask.get(depId);
      if (!up) continue;
      const from = up.role || 'unknown';
      if (from === to) continue;
      const key = `${from}→${to}`;
      let e = edgeMap.get(key);
      if (!e) { e = { from, to, weight: 0, tasks: [] }; edgeMap.set(key, e); edges.push(e); }
      e.weight += 1;
      e.tasks.push(t.id);
    }
  }

  // Agent 之间会互相返工（架构↔后端、前端↔后端），所以图里必然有环。
  // 先 DFS 标出「回边」（成环的那条），分层时忽略；回边照样画，只是标成虚线回路。
  const adj = new Map(nodes.map(n => [n.role, []]));
  for (const e of edges) (adj.get(e.from) || []).push(e.to);
  const visit = new Map();   // 0/undefined 未访问 · 1 在栈上 · 2 已完成
  for (const start of nodes.map(n => n.role)) {
    if (visit.get(start)) continue;
    visit.set(start, 1);
    const stack = [{ role: start, i: 0 }];
    while (stack.length) {
      const top = stack[stack.length - 1];
      const succ = adj.get(top.role) || [];
      if (top.i < succ.length) {
        const next = succ[top.i++];
        const st = visit.get(next) || 0;
        if (st === 0) { visit.set(next, 1); stack.push({ role: next, i: 0 }); }
        else if (st === 1) {
          const back = edges.find(e => e.from === top.role && e.to === next);
          if (back) back.back = true;
        }
      } else {
        visit.set(top.role, 2);
        stack.pop();
      }
    }
  }

  // 在去掉回边的有向无环图上做最长路径分层：层号 = 上游最大层 + 1
  const fwd = edges.filter(e => !e.back);
  const outs = new Map(nodes.map(n => [n.role, []]));
  const indeg = new Map(nodes.map(n => [n.role, 0]));
  for (const e of fwd) { outs.get(e.from).push(e.to); indeg.set(e.to, indeg.get(e.to) + 1); }
  const layerOf = new Map(nodes.map(n => [n.role, 0]));
  const queue = nodes.filter(n => !indeg.get(n.role)).map(n => n.role);
  while (queue.length) {
    const id = queue.shift();
    for (const next of outs.get(id)) {
      layerOf.set(next, Math.max(layerOf.get(next) || 0, (layerOf.get(id) || 0) + 1));
      indeg.set(next, indeg.get(next) - 1);
      if (!indeg.get(next)) queue.push(next);
    }
  }
  // 压缩空层，保证层号从 0 连续排列
  const used = [...new Set(layerOf.values())].sort((a, b) => a - b);
  const compact = new Map(used.map((v, i) => [v, i]));
  for (const [role, v] of layerOf) layerOf.set(role, compact.get(v) || 0);

  const layers = [];
  for (const n of nodes) {
    const li = layerOf.get(n.role) || 0;
    while (layers.length <= li) layers.push([]);
    layers[li].push(n.role);
  }

  // 同层排序：先按上游平均位置（重心），再按下游平均位置收敛两轮
  for (let pass = 0; pass < 2; pass++) {
    for (let li = 1; li < layers.length; li++) {
      const prev = layers[li - 1];
      const centerUp = (role) => {
        const idx = edges.filter(e => e.to === role).map(e => e.from).map(r => prev.indexOf(r)).filter(i => i >= 0);
        return idx.length ? idx.reduce((sum, v) => sum + v, 0) / idx.length : prev.length / 2;
      };
      layers[li].sort((a, b) => centerUp(a) - centerUp(b) || a.localeCompare(b));
    }
    for (let li = layers.length - 2; li >= 0; li--) {
      const next = layers[li + 1];
      const centerDown = (role) => {
        const idx = edges.filter(e => e.from === role).map(e => e.to).map(r => next.indexOf(r)).filter(i => i >= 0);
        return idx.length ? idx.reduce((sum, v) => sum + v, 0) / idx.length : next.length / 2;
      };
      layers[li].sort((a, b) => centerDown(a) - centerDown(b) || a.localeCompare(b));
    }
  }

  const pos = new Map();
  const maxRows = Math.max(1, ...layers.map(l => l.length));
  layers.forEach((ids, li) => {
    const offset = ((maxRows - ids.length) * (AH + AGENT_GAP_Y)) / 2;
    ids.forEach((role, ri) => pos.set(role, { x: li * (AW + AGENT_GAP_X), y: offset + ri * (AH + AGENT_GAP_Y) }));
  });

  return {
    nodes: nodes.sort((a, b) => (layerOf.get(a.role) - layerOf.get(b.role)) || a.role.localeCompare(b.role)),
    byRole, edges, layers, layerOf, pos,
    width: Math.max(1, layers.length) * AW + Math.max(0, layers.length - 1) * AGENT_GAP_X,
    height: maxRows * AH + Math.max(0, maxRows - 1) * AGENT_GAP_Y,
  };
}

export { ACTIVE_STATES, agentTone, buildAgentGraph };
