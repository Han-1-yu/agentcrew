/**
 * 设置页：把五组配置组合成一页，保存时一次性写回主进程。
 * 各组只负责构建自己的 DOM 与局部交互，取值统一走 fields.collectSettings。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { buildAboutGroup } from './about.js';
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
      const next = collectSettings(cfg);
      try {
        await window.studio.setSettings(next);
        S.settings = Object.assign({}, S.settings, next);
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
  wrap.appendChild(buildConnectionGroup(cfg));
  wrap.appendChild(buildAppearanceGroup(cfg));
  wrap.appendChild(buildAboutGroup());
}

export { renderSettingsPage };
