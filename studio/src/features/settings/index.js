/**
 * 设置页：把五组配置组合成一页，保存时一次性写回主进程。
 * 各组只负责构建自己的 DOM 与局部交互，取值统一走 fields.collectSettings。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { buildAboutGroup } from './about.js';
import { buildAgentsGroup } from './agents.js';
import { buildAppearanceGroup } from './appearance.js';
import { buildConnectionGroup } from './connection.js';
import { buildEnvironmentGroup } from './environment.js';
import { collectSettings } from './fields.js';
import { buildGatewayGroup } from './gateway.js';
import { buildModelBudgetGroup } from './model-budget.js';

function buildPageHead(cfg) {
  return h('div', { class: 'page-head' },
    h('h2', {}, '设置'),
    h('button', { class: 'btn btn-primary', onclick: async () => {
      const { values: next, error } = collectSettings(cfg);
      if (error) { toast('error', '设置未保存', error); return; }
      try {
        const prevApi = (S.settings && S.settings.apiBaseUrl) || '';
        const saved = await window.studio.setSettings(next);
        S.settings = saved || Object.assign({}, S.settings, next);
        // 后端地址变了要立刻重建实时通道，否则仍连着旧地址 / 仍是文件监听。
        if (String(next.apiBaseUrl || '') !== String(prevApi)) {
          window.dispatchEvent(new Event('studio:reconnect'));
        }
        toast('success', '已保存', '设置已更新');
      } catch (e) { toast('error', '保存失败', String(e.message || e)); }
    } }, '保存'));
}

async function renderSettingsPage(container) {
  const cfg = S.settings || {};
  container.appendChild(buildPageHead(cfg));

  const wrap = h('div', { class: 'settings-groups' });
  container.appendChild(wrap);

  wrap.appendChild(buildEnvironmentGroup(cfg));
  wrap.appendChild(buildModelBudgetGroup(cfg));
  wrap.appendChild(buildGatewayGroup(cfg));
  wrap.appendChild(buildAgentsGroup());
  wrap.appendChild(buildConnectionGroup(cfg));
  wrap.appendChild(buildAppearanceGroup(cfg));
  wrap.appendChild(buildAboutGroup());
}

export { renderSettingsPage };
