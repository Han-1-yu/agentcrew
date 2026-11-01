/**
 * 协作流视图：执行者 → 质量整合 → 结果去向 的三列泳道。
 *
 * 与「依赖图」互补：依赖图回答"谁等谁"，协作流回答"现在谁在干、汇到哪里、
 * 最后产出什么"。同一个 run 的同一份数据，两种看法。
 *
 * 连线是布局算出来的，不是写死的：卡片是普通 DOM，SVG 只负责画曲线。
 * 因此文字可以自然换行、卡片能随内容长高，不会像纯 SVG 方案那样被坐标锁死。
 */
import { ROLE_META, STATUS_META } from '../../contracts/enums.js';

import { h, s } from '../../lib/dom.js';
import { fmtBytes, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { switchNav } from '../../app/router.js';

import { openAgentDetail, openTaskDetail } from '../detail/panel.js';

//: 左侧分组：把 8 个角色收成 3 组，组内按原始顺序。只显示本 run 有任务的角色。
const GROUPS = [
  { key: 'plan', title: '规划与设计', roles: ['project_manager', 'product_manager', 'architect'] },
  { key: 'build', title: '研发与验证', roles: ['backend_dev', 'frontend_dev', 'qa_engineer', 'devops_engineer'] },
  { key: 'ship', title: '交付', roles: ['technical_writer'] },
];

//: 每个执行者一条固定颜色，连线与中心区圆点共用，扫一眼能把线和行对上。
const COLORS = [
  'var(--sky)', 'var(--purple)', 'var(--success)', 'var(--orange)',
  'var(--indigo)', 'var(--accent)', 'var(--pink)', 'var(--text-ter)',
];

function agentBrief(role) {
  const a = S.dash.agents && S.dash.agents[role];
  const mine = (S.dash.tasks || []).filter(t => t.role === role);
  const running = mine.find(t => ['running', 'review', 'rework'].includes(t.status));
  const done = mine.filter(t => t.status === 'succeeded').length;
  const failed = mine.filter(t => t.status === 'failed').length;
  let label = '待命';
  let tone = 'idle';
  if (running) { label = '运行中'; tone = 'running'; }
  else if (failed) { label = '有失败'; tone = 'failed'; }
  else if (done && done === mine.length) { label = '已完成'; tone = 'done'; }
  return { a, mine, running, done, failed, label, tone };
}

function dot(tone) { return h('span', { class: `flow-dot ${tone}` }); }

function agentCard(role, color) {
  const rm = ROLE_META[role] || { label: role, full: role, color: 'var(--text-ter)' };
  const st = agentBrief(role);
  return h('div', {
    class: 'flow-card flow-agent', 'data-agent': role, style: `--flow-color:${color}`,
    onclick: () => openAgentDetail(role),
    title: st.running ? `${st.running.title}` : rm.label,
  },
    h('div', { class: 'flow-card-head' },
      h('span', { class: 'flow-avatar', style: `background:${rm.color}` }, (rm.label || role).charAt(0)),
      h('div', { class: 'flow-card-id' },
        h('div', { class: 'flow-card-name' }, rm.full || rm.label),
        h('div', { class: 'flow-card-sub' }, st.running ? trunc(st.running.title, 24) : `${rm.label} · ${st.done} 项完成`)),
      h('span', { class: `flow-state ${st.tone}` }, dot(st.tone), st.label)),
    h('div', { class: 'flow-card-foot' },
      h('span', { class: 'flow-tag' }, rm.label),
      h('span', { class: 'flow-tag muted' }, st.a ? st.a.model || '—' : '—')));
}

function leftColumn() {
  const tasks = S.dash.tasks || [];
  const active = new Set(tasks.map(t => t.role));
  const groups = GROUPS
    .map(g => ({ ...g, roles: g.roles.filter(r => active.has(r)) }))
    .filter(g => g.roles.length);
  const total = groups.reduce((n, g) => n + g.roles.length, 0);
  if (!total) return h('div', { class: 'flow-empty' }, '本次运行还没有分配给执行者的任务');

  let idx = 0;
  return h('div', { class: 'flow-col-body' },
    ...groups.map(g => h('div', { class: 'flow-group' },
      h('div', { class: 'flow-group-head' },
        h('span', {}, g.title), h('span', { class: 'flow-count' }, `${g.roles.length} 个执行者`)),
      ...g.roles.map(r => agentCard(r, COLORS[idx++ % COLORS.length])))));
}

function centerRows() {
  const tasks = S.dash.tasks || [];
  const present = new Set(tasks.map(t => t.role));
  // 行序必须与左列分组顺序一致：否则"第一个执行者"的线会横穿整张卡连到最下面
  const active = GROUPS.flatMap(g => g.roles.filter(r => present.has(r)));
  if (!active.length) return [h('div', { class: 'flow-row muted' }, '等待任务派发')];
  return active.map((role, i) => {
    const rm = ROLE_META[role] || { label: role, full: role, color: 'var(--text-ter)' };
    const st = agentBrief(role);
    return h('div', { class: 'flow-row', 'data-in': role },
      h('span', { class: 'flow-port-dot', style: `background:${COLORS[i % COLORS.length]}` }),
      h('span', { class: 'flow-row-name' }, rm.label),
      h('span', { class: `flow-row-state ${st.tone}` }, st.label));
  });
}

function outputPaths() {
  const out = [];
  for (const t of S.dash.tasks || []) {
    for (const a of ((t.result && t.result.artifacts) || [])) {
      if (a && a.path) out.push(a);
    }
  }
  return out;
}

function rightColumn() {
  const arts = outputPaths();
  const hasDelivery = arts.some(a => /^DELIVERY\.md$/i.test(a.path));
  const top = arts.slice(0, 3);
  const running = (S.dash.run && S.dash.run.status) === 'running';

  return h('div', { class: 'flow-col-body' },
    h('div', { class: 'flow-card flow-out', 'data-out': 'manifest', onclick: () => switchNav('artifacts') },
      h('div', { class: 'flow-card-head' },
        h('div', { class: 'flow-card-id' },
          h('div', { class: 'flow-card-name' }, '产物清单'),
          h('div', { class: 'flow-card-sub' }, `MANIFEST · ${arts.length} 个文件`))),
      h('div', { class: 'flow-file-list' },
        ...(top.length
          ? top.map(a => h('div', { class: 'flow-file' },
              h('span', { class: 'flow-file-name' }, trunc(a.path, 26)),
              h('span', { class: 'flow-file-size' }, fmtBytes(a.size_bytes || 0))))
          : h('div', { class: 'flow-file muted' }, '暂无产物'))),
      h('div', { class: 'flow-card-foot muted' }, running ? '等待本轮产物落盘' : (arts.length ? '已封版' : '等待首个产物'))),

    h('div', { class: 'flow-card flow-out', 'data-out': 'delivery', onclick: () => switchNav('artifacts') },
      h('div', { class: 'flow-card-head' },
        h('div', { class: 'flow-card-id' },
          h('div', { class: 'flow-card-name' }, '交付包'),
          h('div', { class: 'flow-card-sub' }, 'DELIVERY · 汇总与索引'))),
      h('div', { class: 'flow-file-list' },
        h('div', { class: 'flow-file' },
          h('span', { class: 'flow-file-name' }, 'DELIVERY.md'),
          h('span', { class: `flow-file-mark ${hasDelivery ? 'ok' : ''}` }, hasDelivery ? '已生成' : '待生成')),
        h('div', { class: 'flow-file' },
          h('span', { class: 'flow-file-name' }, 'MANIFEST.json'),
          h('span', { class: `flow-file-mark ${arts.length ? 'ok' : ''}` }, arts.length ? '已生成' : '待生成'))),
      h('div', { class: 'flow-card-foot muted' }, hasDelivery ? '交付包已就绪' : '等待 T-FINAL 汇总')),
  );
}

function centerCard() {
  const run = S.dash.run || {};
  const meta = STATUS_META[run.phase === 'delivery' ? 'succeeded' : 'running'] || {};
  return h('div', { class: 'flow-core' },
    h('div', { class: 'flow-core-head' },
      h('div', { class: 'flow-core-logo' }, '⚖'),
      h('div', {},
        h('div', { class: 'flow-core-name' }, '质量门禁'),
        h('div', { class: 'flow-core-sub' }, '静态校验 · LLM 评审 · 交付汇总')),
    ),
    h('div', { class: 'flow-core-label' }, '当前会话'),
    h('div', { class: 'flow-core-rows' }, ...centerRows()),
    h('div', { class: 'flow-core-label' }, '产出'),
    h('div', { class: 'flow-core-rows' },
      h('div', { class: 'flow-row out', 'data-out-row': 'manifest' },
        h('span', { class: 'flow-port-dot', style: 'background:var(--success)' }),
        h('span', { class: 'flow-row-name' }, '结构化产物'),
        h('span', { class: 'flow-row-state done' }, meta.label || '进行中')),
      h('div', { class: 'flow-row out', 'data-out-row': 'delivery' },
        h('span', { class: 'flow-port-dot', style: 'background:var(--success)' }),
        h('span', { class: 'flow-row-name' }, '报告交付包'),
        h('span', { class: 'flow-row-state done' }, '汇总'))),
    h('div', { class: 'flow-core-foot' }, '— 任务产物汇入，评审结果分流'));
}

/** 画连线：卡片右缘 → 中心区对应行左缘；中心区产出行 → 右侧卡片左缘。 */
function drawLinks(root) {
  const svg = root.querySelector('.flow-links');
  if (!svg) return;
  const box = root.getBoundingClientRect();
  if (!box.width || !box.height) return;
  svg.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  svg.setAttribute('width', box.width);
  svg.setAttribute('height', box.height);
  svg.innerHTML = '';

  const at = el => {
    const r = el.getBoundingClientRect();
    return { left: r.left - box.left, right: r.right - box.left, cy: r.top - box.top + r.height / 2 };
  };
  const curve = (x1, y1, x2, y2, color) => {
    const dx = Math.max(28, (x2 - x1) * 0.55);
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`);
    p.setAttribute('class', 'flow-link');
    p.setAttribute('style', `stroke:${color}`);
    return p;
  };
  const port = (x, y, color) => {
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('r', 2.6);
    c.setAttribute('class', 'flow-port'); c.setAttribute('style', `fill:${color}`);
    return c;
  };

  const frag = document.createDocumentFragment();
  root.querySelectorAll('.flow-agent').forEach(card => {
    const role = card.dataset.agent;
    const row = root.querySelector(`.flow-row[data-in="${role}"]`);
    if (!row) return;
    const color = (card.style.getPropertyValue('--flow-color') || 'var(--ink-25)').trim();
    const a = at(card), b = at(row);
    const path = curve(a.right, a.cy, b.left - 1, b.cy, color);
    path.dataset.agent = role;
    frag.appendChild(path);
    frag.appendChild(port(a.right, a.cy, color));
    frag.appendChild(port(b.left - 1, b.cy, color));
  });
  root.querySelectorAll('.flow-out').forEach(card => {
    const key = card.dataset.out;
    const row = root.querySelector(`.flow-row[data-out-row="${key}"]`);
    if (!row) return;
    const color = 'var(--success)';
    const a = at(row), b = at(card);
    frag.appendChild(curve(a.right + 1, a.cy, b.left, b.cy, color));
  });
  svg.appendChild(frag);
}

function wireInteractions(root) {
  root.querySelectorAll('.flow-agent').forEach(card => {
    const role = card.dataset.agent;
    card.addEventListener('mouseenter', () => {
      root.classList.add('flow-focus');
      card.classList.add('hot');
      const row = root.querySelector(`.flow-row[data-in="${role}"]`);
      if (row) row.classList.add('hot');
      root.querySelectorAll('.flow-link').forEach(p => {
        if (p.dataset.agent === role) p.classList.add('hot');
      });
    });
    card.addEventListener('mouseleave', () => {
      root.classList.remove('flow-focus');
      card.classList.remove('hot');
      root.querySelectorAll('.flow-row.hot, .flow-link.hot').forEach(el => el.classList.remove('hot'));
    });
  });
}

function renderFlowView(container) {
  const wrap = h('div', { class: 'flow-wrap' },
    // 必须用 s()（SVG 命名空间）：h() 建出来的是 HTMLUnknownElement，
    // 里面的 path 不会被当成图形渲染，bbox 恒为 0。
    s('svg', { class: 'flow-links', 'aria-hidden': 'true' }),
    h('div', { class: 'flow-cols' },
      h('section', { class: 'flow-col' },
        h('div', { class: 'flow-col-head' }, '任务执行者', h('span', { class: 'flow-count' }, String((S.dash.tasks || []).length ? new Set((S.dash.tasks || []).map(t => t.role)).size : 0))),
        leftColumn()),
      h('section', { class: 'flow-col mid' },
        h('div', { class: 'flow-col-head' }, '质量整合'),
        centerCard()),
      h('section', { class: 'flow-col right' },
        h('div', { class: 'flow-col-head' }, '结果去向', h('span', { class: 'flow-count' }, '2')),
        rightColumn())));
  container.appendChild(wrap);
  // 布局完成后再画线（等字体/尺寸稳定一帧）
  requestAnimationFrame(() => drawLinks(wrap));
  wireInteractions(wrap);
  const ro = new ResizeObserver(() => {
    if (!wrap.isConnected) { ro.disconnect(); return; }
    drawLinks(wrap);
  });
  ro.observe(wrap);
  // 视图切走时容器被清空，但 observer 仍挂在游离节点上——把释放动作挂到容器上，
  // 由切视图的一方调用（没有 hook 的话它就永久留在内存里）。
  wrap._flowDispose = () => ro.disconnect();
  return wrap;
}

export { drawLinks, renderFlowView };
