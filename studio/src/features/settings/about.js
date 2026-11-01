/**
 * 关于区块：版本定位 + 快捷键速查（纯静态）。
 */
import { h } from '../../lib/dom.js';

function buildAboutGroup() {
  return h('section', { class: 'settings-group settings-about-group' },
    h('h3', { class: 'settings-group-title' }, '关于'),
    h('div', { class: 'settings-about' },
      'AgentCrew Studio · 多 Agent 协作开发控制台',
      h('br'), '8 角色协作 · 三层门禁 · DAG 编排 · 实时可观测',
      h('br'), h('br'),
      h('span', { style: 'font-weight:600;color:var(--text-sub)' }, '快捷键'),
      h('br'), 'Esc — 关闭面板 / 弹窗',
      h('br'), 'Ctrl/⌘ + N — 新建运行',
      h('br'), '1-9 — 切换页面（总览/计划/任务图/Agents/门禁/产物/事件/设置/供应商）'));
}

export { buildAboutGroup };
