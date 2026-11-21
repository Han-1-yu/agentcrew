/**
 * Q2：工具试跑页（P1）。R24 工具实验台的前端入口——清单 / 开关口径 / 单工具试跑。
 *
 * 端点：GET /tools（清单 + tools_enabled / code_exec_enabled / sandbox_backend /
 * search_provider / trial_dir）、POST /tools/{name}/test（R24 沙箱试跑）。
 * 两条边界照抄后端：未连 API 给诚实空态；tools_enabled=false 时按钮禁用并说明
 * 「试跑会被 409 拒绝」，不把清单页装成能跑。
 */
import { apiFetch } from '../../lib/api.js';
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';
import { kindLabel, statusChips } from '../../lib/tool-lab.js';

import { emptyState, errorState, loadingState } from '../../ui/states.js';

import { openTrialDialog } from './dialog.js';

function explain(err) {
  return String((err && (err.detail || err.message)) || err || '未知错误');
}

function fmtSchema(tool) {
  const schema = tool.input_schema || {};
  const required = schema.required || [];
  const props = Object.keys(schema.properties || {});
  if (!props.length) return '无入参';
  return required.length
    ? `必填：${required.join(', ')}`
    : `可选：${props.join(', ')}`;
}

function toolCard(tool, { enabled, onTrial }) {
  const badgeCls = `tool-card-badge ${tool.kind || 'other'}`;
  return h('div', { class: 'tool-card' },
    h('div', { class: 'tool-card-head' },
      h('span', { class: 'tool-card-name mono' }, tool.name),
      h('span', { class: badgeCls }, kindLabel(tool.kind)),
      h('span', { class: 'tool-card-meta' }, fmtSchema(tool))),
    h('div', { class: 'tool-card-desc' }, tool.description || '（后端未提供说明）'),
    h('div', { class: 'tool-card-actions' },
      h('button', {
        class: 'btn btn-sm btn-primary', type: 'button', disabled: !enabled,
        title: enabled ? '在独立沙箱里试跑一次' : '工具执行未开启（tools_enabled=false）',
        onclick: () => onTrial(tool),
      }, '试跑')));
}

function renderChips(box, payload) {
  box.replaceChildren(...statusChips(payload).map((chip) =>
    h('span', { class: `tool-chip ${chip.tone}` }, chip.label)));
}

async function load(box, chipsBox, { token }) {
  box.replaceChildren(loadingState('正在读取工具清单…'));
  let payload;
  try {
    payload = await apiFetch('/tools');
  } catch (err) {
    if (token !== S.navToken) return;
    box.replaceChildren(errorState('工具清单读取失败', explain(err),
      () => renderToolsPage(document.getElementById('placeholder-view'))));
    return;
  }
  if (token !== S.navToken || !box.isConnected) return;
  renderChips(chipsBox, payload);
  const tools = (payload && payload.tools) || [];
  const enabled = !!(payload && payload.tools_enabled);
  if (!tools.length) {
    box.replaceChildren(emptyState('清单里没有可试跑的工具',
      enabled
        ? '后端这次没有注册任何工具：检查 code_exec_enabled（run_python / run_tests）与 search_provider（web_search）。'
        : '工具执行未开启（tools_enabled=false）：开启后清单里才会出现内置工具（安全边界见 OPERATIONS §3.3）。'));
    return;
  }
  box.replaceChildren(...tools.map((tool) =>
    toolCard(tool, { enabled, onTrial: (t) => openTrialDialog(t, { enabled }) })));
}

function renderToolsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '工具试跑'),
      h('div', { class: 'plan-goal' },
        'R24 工具实验台：把注册表里的工具单独拎出来在沙箱里试跑，入参校验与 Agent 调用同一套语义'))));

  if (!S.realMode || !(S.settings && S.settings.apiBaseUrl)) {
    container.appendChild(emptyState('需要连接后端 API',
      '工具清单与试跑走 REST（GET /tools、POST /tools/{name}/test）：请在「设置 → 连接」配置后端地址。'));
    return;
  }

  container.appendChild(h('div', { class: 'tool-toolsbar' },
    h('button', {
      class: 'btn btn-sm', type: 'button',
      onclick: () => renderToolsPage(document.getElementById('placeholder-view')),
    }, '刷新')));

  const chipsBox = h('div', { class: 'tool-chips' });
  const box = h('div', { class: 'tool-list' });
  container.appendChild(chipsBox);
  container.appendChild(box);
  load(box, chipsBox, { token: S.navToken }).catch((err) => {
    box.replaceChildren(errorState('工具清单读取失败', explain(err)));
  });
}

export { renderToolsPage };
