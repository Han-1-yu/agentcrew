/**
 * 设置页「快捷键」组（O1）：逐个动作录制新键位、即时查冲突、按动作恢复默认。
 * 取值走 `data-spec` 属性，`collectSettings` 直接读行上的值，不另存草稿。
 */
import { $$, h } from '../../lib/dom.js';
import { actionDefs, displaySpec, resolveShortcuts, shortcutConflicts, specFromEvent } from '../../lib/shortcuts.js';

function currentMap() {
  const map = {};
  $$('.shortcut-row').forEach((row) => { map[row.dataset.action] = row.dataset.spec; });
  return map;
}

function refreshConflictHint(box) {
  const conflicts = shortcutConflicts(currentMap());
  const defs = Object.fromEntries(actionDefs().map((a) => [a.id, a.label]));
  box.textContent = conflicts.length
    ? `键位冲突：${conflicts.map((c) => `${displaySpec(c.spec)} 同时绑给了「${defs[c.a]}」与「${defs[c.b]}」`).join('；')}`
    : '';
  box.classList.toggle('is-error', conflicts.length > 0);
}

function buildRow(def, spec) {
  const code = h('span', { class: 'shortcut-spec' });
  const row = h('div', { class: 'shortcut-row', 'data-action': def.id, 'data-spec': spec },
    h('div', { class: 'shortcut-meta' },
      h('span', { class: 'shortcut-label' }, def.label),
      h('span', { class: 'shortcut-hint' }, def.hint)),
    code,
    h('button', { class: 'btn btn-sm', type: 'button', title: '按一下给这个动作重新分配键位' }, '录制'),
    h('button', { class: 'btn btn-sm', type: 'button', title: '恢复默认键位' }, '默认'));
  const paint = () => {
    code.textContent = displaySpec(row.dataset.spec);
    row.classList.remove('is-recording');
    row.dataset.recording = '';
  };
  paint();

  const [recordBtn, resetBtn] = $$('button', row);
  // 录制：接管下一次 keydown（Esc 取消），不弹窗、不抢已有面板
  recordBtn.addEventListener('click', () => {
    row.dataset.recording = '1';
    row.classList.add('is-recording');
    code.textContent = '按新组合…';
    const finish = () => {
      window.removeEventListener('keydown', onKey, true);
      paint();
      refreshConflictHint(row.closest('.shortcut-group').querySelector('.shortcut-conflict'));
    };
    const onKey = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.key === 'Escape') { finish(); return; }
      const next = specFromEvent(ev);
      if (next) row.dataset.spec = next;
      finish();
    };
    window.addEventListener('keydown', onKey, true);
  });
  resetBtn.addEventListener('click', () => {
    row.dataset.spec = def.def;
    paint();
    refreshConflictHint(row.closest('.shortcut-group').querySelector('.shortcut-conflict'));
  });
  return row;
}

function buildShortcutsGroup(cfg) {
  const map = resolveShortcuts(cfg && cfg.shortcuts);
  const conflict = h('div', { class: 'shortcut-conflict' });
  const group = h('section', { class: 'settings-group shortcut-group' },
    h('h3', { class: 'settings-group-title' }, '快捷键'),
    h('div', { class: 'shortcut-note' },
      '数字键 1-9 切页固定不可改；这里只收带修饰键的组合或 F1-F12（裸字母会劫持输入框）。点「录制」后按一下新组合，Esc 取消。'),
    ...actionDefs().map((a) => buildRow(a, map[a.id])),
    conflict);
  queueMicrotask(() => refreshConflictHint(conflict));
  return group;
}

export { buildShortcutsGroup };
