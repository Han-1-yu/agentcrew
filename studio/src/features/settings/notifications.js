/**
 * 设置页「通知」组（O2）：系统通知开关、静默时段、最低级别。
 * 开关即时写设置（失败回滚）；时段与级别随「保存」一起提交，由 collectSettings 读取。
 */
import { h } from '../../lib/dom.js';
import { CATEGORIES, LEVELS, normalizePrefs } from '../../lib/notify-prefs.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

function buildNotificationsGroup(cfg) {
  const prefs = normalizePrefs(cfg && cfg.notifyPrefs);
  const system = h('input', { id: 'set-notify-system', type: 'checkbox', checked: prefs.system });
  const from = h('input', { id: 'set-notify-from', class: 'set-input set-input-sm', type: 'time', value: prefs.quietFrom });
  const to = h('input', { id: 'set-notify-to', class: 'set-input set-input-sm', type: 'time', value: prefs.quietTo });
  const level = h('select', { id: 'set-notify-level', class: 'set-input set-input-sm' },
    ...LEVELS.map((l) => h('option', { value: l.id, selected: l.id === prefs.minLevel }, l.label)));
  // 三类目逐项静音：勾上=不再进面板、也不再弹系统通知
  const muteInputs = CATEGORIES.map((c) => {
    const input = h('input', { id: `set-notify-mute-${c.id}`, type: 'checkbox', checked: prefs.mute[c.id] === true });
    return h('label', { class: 'set-check' }, input, h('span', {}, `不提示${c.label}`));
  });

  // 静默时段两端要么都填、要么都空；只填一头视为无效输入，提示后不落盘
  const hint = h('div', { class: 'set-hint' },
    '静默时段内不弹系统通知（面板与角标照常）；设为 22:00–08:00 这类跨零点区间也行。');

  system.addEventListener('change', async () => {
    const next = { ...normalizePrefs(S.settings && S.settings.notifyPrefs), system: system.checked };
    try {
      const saved = await window.studio.setSettings({ notifyPrefs: next });
      S.settings = saved || Object.assign({}, S.settings, { notifyPrefs: next });
      toast('success', '通知设置已更新', system.checked ? '已开启系统通知' : '已关闭系统通知');
    } catch (e) {
      system.checked = !system.checked;
      toast('error', '保存失败', String((e && e.message) || e));
    }
  });

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '通知'),
    h('div', { class: 'settings-field' },
      h('label', { class: 'set-check' }, system, h('span', {}, '窗口失焦时弹系统通知'))),
    h('div', { class: 'settings-field' },
      h('label', {}, '静默时段'),
      h('div', { class: 'set-row' }, from, h('span', { class: 'set-row-label' }, '至'), to)),
    h('div', { class: 'settings-field' },
      h('label', {}, '静默类目'),
      h('div', { class: 'set-check-row' }, ...muteInputs)),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-notify-level' }, '最低级别'),
      level,
      hint));
}

export { buildNotificationsGroup };
