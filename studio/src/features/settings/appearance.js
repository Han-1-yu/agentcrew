/**
 * 组 5「外观」：主题分段控件，点击即时生效并持久化，失败回滚。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';
import { toast } from '../../ui/toast.js';

import { THEME_LABEL, applyTheme } from '../../app/shell.js';

function buildAppearanceGroup() {
  const segEl = h('div', { class: 'seg', id: 'set-theme' });
  const themeButtons = {};

  const renderSeg = () => {
    const cur = (S.settings && S.settings.theme) || 'dark';
    Object.entries(themeButtons).forEach(([t, b]) => b.classList.toggle('active', t === cur));
  };

  ['light', 'dark', 'system'].forEach((t) => {
    const b = h('button', { type: 'button', 'data-theme': t, onclick: async () => {
      const prev = (S.settings && S.settings.theme) || 'dark';
      S.settings.theme = t;
      applyTheme(); renderSeg();
      try {
        await window.studio.setSettings({ theme: t });
        toast('success', '主题已切换', THEME_LABEL[t] || t);
      } catch (e) {
        S.settings.theme = prev;   // 持久化失败回滚
        applyTheme(); renderSeg();
        toast('error', '主题保存失败', String(e && e.message || e));
      }
    } }, THEME_LABEL[t] || t);
    themeButtons[t] = b;
    segEl.appendChild(b);
  });
  renderSeg();

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '外观'),
    h('div', { class: 'settings-field' },
      h('label', {}, '主题'),
      segEl));
}

export { buildAppearanceGroup };
