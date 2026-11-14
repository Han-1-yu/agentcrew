/**
 * Agent 视图：每张卡是一个执行者，边是跨 Agent 的依赖流。
 *
 * 视觉编码：角色色 = 谁，状态色 = 现在怎么样，线宽 = 这条依赖上有几个任务，
 * 箭头 = 依赖方向。回环依赖（返工）走卡片下方的虚线，不和正向流抢道。
 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';

import { $, s } from '../../lib/dom.js';
import { trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { openAgentDetail, openTaskDetail } from '../detail/panel.js';

import { AGENT_GAP_X, AH, AW, arrowDefs, resetAnimIfNewRun, seenNodes, svgText, wireDAGControls } from './dag-canvas.js';
import { agentTone, buildAgentGraph } from './agent-graph.js';

const CHIP_PRIORITY = { running: 0, review: 0, rework: 0, failed: 1, ready: 2, blocked: 3, pending: 4, succeeded: 5, skipped: 6, cancelled: 7 };

function renderAgentDAG(svg, tasks, graph = null) {
  const L = graph || buildAgentGraph(tasks);
  resetAnimIfNewRun();
  svg.innerHTML = '';

  const pad = 54;
  const totalW = L.width + pad * 2;
  const totalH = L.height + pad * 2;
  svg.setAttribute('viewBox', `0 0 ${totalW} ${totalH}`);
  // 画布贴合图形比例（而不是固定高度）：宽窗口里节点能占满卡片宽度，
  // 不留大片信箱边；矮窗口由 CSS 的 max-height 兜住。
  const wrap = $('#dag-canvas-wrap');
  if (wrap) { wrap.style.aspectRatio = `${totalW} / ${totalH}`; wrap.style.height = ''; }

  const defs = s('defs', {});
  arrowDefs(defs, L.nodes.map(n => n.role));
  // 每张卡一枚柔光渐变：中性白，只做「顶部柔和高光」，不按角色染色
  L.nodes.forEach((_, i) => {
    defs.appendChild(s('radialGradient', { id: `agent-sheen-${i}`, cx: '16%', cy: '-12%', r: '96%' },
      s('stop', { offset: '0%', 'stop-color': '#FFFFFF', 'stop-opacity': '0.10' }),
      s('stop', { offset: '58%', 'stop-color': '#FFFFFF', 'stop-opacity': '0.02' }),
      s('stop', { offset: '100%', 'stop-color': '#FFFFFF', 'stop-opacity': '0' })));
  });
  svg.appendChild(defs);

  const vp = s('g', { id: 'dag-viewport', transform: `translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})` });

  // 层级带：先画底，给整张图一根「从上游到交付」的骨架
  L.layers.forEach((ids, li) => {
    const x = pad + li * (AW + AGENT_GAP_X) - 20;
    vp.appendChild(s('rect', { class: 'dag-tier', x, y: pad - 26, width: AW + 40, height: L.height + 52, rx: 20 }));
    vp.appendChild(svgText('dag-tier-label', x + 12, pad - 9, `第 ${li + 1} 层`, { 'font-size': 9.5, 'letter-spacing': '1.4' }));
    vp.appendChild(svgText('dag-tier-ghost', x + AW + 26, pad - 7, String(li + 1).padStart(2, '0'),
      { 'font-size': 19, 'text-anchor': 'end', 'font-family': 'var(--font-display)' }));
  });

  // 连线：角色色 = 上游 Agent，箭头 = 依赖方向，线宽 = 该方向上的依赖任务数
  for (const e of L.edges) {
    const from = L.pos.get(e.from), to = L.pos.get(e.to);
    if (!from || !to) continue;
    const x1 = pad + from.x + AW, y1 = pad + from.y + AH / 2;
    const x2 = pad + to.x - 1, y2 = pad + to.y + AH / 2;
    const dx = Math.max(46, Math.min(150, (x2 - x1) * 0.55));
    const target = L.byRole.get(e.to);
    const kind = target && target.active ? 'live'
      : target && target.failed ? 'failed'
      : target && target.total && target.done === target.total ? 'done' : 'idle';
    const color = 'var(--dag-edge)';
    const width = (1.6 + Math.min(3.2, (e.weight - 1) * 1.1)).toFixed(2);
    // 回环依赖（下游又反哺上游，如后端返工架构）走卡片下方的大弧线，
    // 不和正向边挤在同一条直线上，环一眼可见。
    const baseY = pad + L.height + 30;
    const d = e.back
      ? `M ${x1} ${y1} C ${x1 + 54} ${baseY}, ${x2 - 54} ${baseY}, ${x2} ${y2}`
      : `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;

    const path = s('path', {
      class: `dag-edge agent-edge agent-${kind}${e.back ? ' agent-back' : ''}`,
      d, fill: 'none', 'stroke-width': width,
      'marker-end': `url(#agent-arrow-${e.from})`,
      'data-from-role': e.from, 'data-to-role': e.to,
      'data-weight': e.weight, 'data-loop': e.back ? '1' : '0',
      style: `--edge-color:${color}`,
    });
    path.appendChild(s('title', {}, `${roleMeta(e.from).full} → ${roleMeta(e.to).full} · ${e.weight} 个任务${e.back ? ' · 回环依赖' : ''}`));
    vp.appendChild(path);

    if (e.weight > 1) {
      const mx = (x1 + x2) / 2;
      const my = e.back ? baseY : (y1 + y2) / 2;
      vp.appendChild(s('rect', { class: 'edge-weight-bg', x: mx - 11, y: my - 7, width: 22, height: 15, rx: 7.5 }));
      vp.appendChild(svgText('edge-weight', mx, my + 4, `×${e.weight}`, { 'font-size': 8.5, 'text-anchor': 'middle', 'font-family': 'var(--font-mono)' }));
    }
  }

  // 节点卡
  let seq = 0;
  L.nodes.forEach((n, i) => {
    const p = L.pos.get(n.role);
    if (!p) return;
    const rm = roleMeta(n.role);
    const tone = agentTone(n);
    const toneColor = (STATUS_META[tone] || STATUS_META.unknown).color;
    const toneLabel = {
      running: '执行中', failed: '有失败', blocked: '受阻', succeeded: '已完成', ready: '排队中', pending: '待命',
    }[tone] || '待命';
    const fresh = !seenNodes.has('agent:' + n.role);
    seenNodes.add('agent:' + n.role);

    const node = s('g', {
      class: `dag-node agent-node${n.active ? ' running' : ''}${fresh ? ' enter' : ''}`,
      transform: `translate(${pad + p.x} ${pad + p.y})`,
      'data-agent': n.role, 'data-role': n.role, 'data-status': tone,
      style: `--i:${seq++};--role-color:${rm.color};--tone-color:${toneColor}`,
    });
    const body = s('g', { class: 'node-body' });

    body.appendChild(s('rect', { class: 'node-box', width: AW, height: AH, rx: 16, 'stroke-width': 1.3 }));
    body.appendChild(s('rect', { class: 'node-sheen', width: AW, height: AH, rx: 16, fill: `url(#agent-sheen-${i})` }));
    body.appendChild(s('rect', { class: 'node-inner', x: 4, y: 4, width: AW - 8, height: AH - 8, rx: 13 }));
    body.appendChild(s('rect', { class: 'node-accent', x: 0, y: 22, width: 3.5, height: AH - 44, rx: 1.75 }));

    // 头像 + 名称 + 角色/模型
    body.appendChild(s('circle', { class: 'node-avatar-ring', cx: 32, cy: 33, r: 20 }));
    body.appendChild(s('circle', { class: 'node-avatar', cx: 32, cy: 33, r: 15.5 }));
    body.appendChild(svgText('node-on-solid', 32, 37.5, (rm.label || '?').charAt(0),
      { 'font-size': 11.5, 'font-weight': 700, 'text-anchor': 'middle' }));
    body.appendChild(svgText('node-title', 56, 28, trunc(rm.full || rm.label, 9), { 'font-size': 14.5 }));
    const model = (S.dash.agents && S.dash.agents[n.role] && S.dash.agents[n.role].model) || '未绑定执行体';
    body.appendChild(svgText('node-sub', 56, 45, trunc(`${rm.label} · ${model}`, 18), { 'font-size': 9.5 }));

    // 状态：一颗小圆点 + 极淡文字，不再用整块胶囊抢版面
    body.appendChild(s('circle', { class: 'agent-state-dot', cx: AW - 24, cy: 26, r: 2.8 }));
    body.appendChild(svgText('agent-state-text', AW - 32, 29.5, toneLabel,
      { 'font-size': 9, 'text-anchor': 'end' }));

    body.appendChild(s('line', { class: 'agent-hairline', x1: 16, y1: 62, x2: AW - 16, y2: 62 }));

    // 三段指标：任务 / 完成 / 进行（数值大、标签小，一眼扫出量级）
    const metrics = [
      { x: 18, value: n.total, label: '任务' },
      { x: 88, value: n.done, label: '完成' },
      { x: 158, value: n.active, label: '进行', hot: n.active > 0 },
    ];
    metrics.forEach((m, mi) => {
      const val = String(m.value);
      body.appendChild(svgText('metric-value', m.x, 84, val, { 'font-size': 17 }));
      body.appendChild(svgText('metric-label' + (m.hot ? ' hot' : ''), m.x + val.length * 9.5 + 5, 84, m.label, { 'font-size': 9.5 }));
      if (mi < metrics.length - 1) body.appendChild(s('line', { class: 'agent-hairline', x1: m.x + 56, y1: 71, x2: m.x + 56, y2: 87 }));
    });

    // 任务 chip：优先展示在跑/失败的，点击直达任务详情
    const picks = [...n.tasks]
      .sort((a, b) => (CHIP_PRIORITY[a.status] ?? 9) - (CHIP_PRIORITY[b.status] ?? 9) || String(a.id).localeCompare(String(b.id)))
      .slice(0, 2);
    let chipX = 18;
    const chipY = 100;
    for (const t of picks) {
      const w = Math.max(38, 12 + String(t.id).length * 5.4);
      const chip = s('g', { class: 'agent-task', 'data-task-id': t.id, transform: `translate(${chipX} ${chipY})` });
      chip.appendChild(s('rect', { width: w, height: 16, rx: 8 }));
      chip.appendChild(svgText('agent-task-text', w / 2, 11, t.id, { 'font-size': 9, 'text-anchor': 'middle', 'font-family': 'var(--font-mono)' }));
      chip.appendChild(s('title', {}, `${t.id} · ${t.title}`));
      chip.style.cursor = 'pointer';
      chip.addEventListener('click', (evt) => { evt.stopPropagation(); openTaskDetail(t.id); });
      body.appendChild(chip);
      chipX += w + 6;
    }
    if (n.tasks.length > picks.length && chipX < AW - 40) {
      const rest = n.tasks.length - picks.length;
      body.appendChild(svgText('agent-more', chipX + 2, 111, `+${rest}`, { 'font-size': 9, 'font-family': 'var(--font-mono)' }));
    }

    // 完成率
    const ratio = n.total ? n.done / n.total : 0;
    body.appendChild(s('rect', { class: 'agent-progress-track', x: 18, y: 138, width: AW - 36, height: 3, rx: 1.5 }));
    body.appendChild(s('rect', { class: 'agent-progress-fill', x: 18, y: 138, width: Math.max(2, (AW - 36) * ratio), height: 3, rx: 1.5 }));
    body.appendChild(svgText('agent-progress-text', AW - 18, 131, `${n.done}/${n.total} 完成`,
      { 'font-size': 9, 'text-anchor': 'end' }));

    // 悬停 / 点击
    node.addEventListener('mouseenter', () => {
      svg.classList.add('dag-focus');
      node.classList.add('is-focus');
      const near = new Set([n.role]);
      vp.querySelectorAll('.agent-edge').forEach(edge => {
        const up = edge.dataset.fromRole, down = edge.dataset.toRole;
        if (up === n.role || down === n.role) {
          edge.classList.add('edge-focus');
          near.add(up); near.add(down);
        }
      });
      vp.querySelectorAll('.agent-node').forEach(other => {
        if (near.has(other.dataset.agent)) other.classList.add('is-near');
      });
    });
    node.addEventListener('mouseleave', () => {
      svg.classList.remove('dag-focus');
      node.classList.remove('is-focus');
      vp.querySelectorAll('.agent-edge.edge-focus').forEach(edge => edge.classList.remove('edge-focus'));
      vp.querySelectorAll('.agent-node.is-near').forEach(other => other.classList.remove('is-near'));
    });
    node.addEventListener('click', () => {
      if (S.dash.agents && S.dash.agents[n.role]) openAgentDetail(n.role);
      else if (n.tasks[0]) openTaskDetail(n.tasks[0].id);
    });

    node.appendChild(body);
    vp.appendChild(node);
  });

  svg.appendChild(vp);
  wireDAGControls(svg, vp, totalW, totalH);
}

export { renderAgentDAG };
