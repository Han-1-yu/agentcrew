import { test } from 'node:test';
import assert from 'node:assert';

import {
  actionDefs, displaySpec, matches, normalizeSpec, resolveShortcuts, shortcutConflicts, specFromEvent,
} from '../src/lib/shortcuts.js';

test('normalizeSpec：Mod/Alt/Shift 与功能键', () => {
  assert.deepEqual(normalizeSpec('Mod+K'), { mod: true, alt: false, shift: false, key: 'K' });
  assert.deepEqual(normalizeSpec('Alt+Shift+p'), { mod: false, alt: true, shift: true, key: 'P' });
  assert.deepEqual(normalizeSpec('F5'), { mod: false, alt: false, shift: false, key: 'F5' });
});

test('normalizeSpec：裸字母 / 空串不合法（避免劫持输入框）', () => {
  assert.equal(normalizeSpec('K'), null);
  assert.equal(normalizeSpec(''), null);
  assert.equal(normalizeSpec(null), null);
  assert.equal(normalizeSpec('Shift'), null);
});

test('specFromEvent：记录组合键，忽略只按修饰键', () => {
  assert.equal(specFromEvent({ key: 'k', ctrlKey: true }), 'Mod+K');
  assert.equal(specFromEvent({ key: 'K', metaKey: true, shiftKey: true }), 'Mod+Shift+K');
  assert.equal(specFromEvent({ key: 'F5' }), 'F5');
  assert.equal(specFromEvent({ key: 'a' }), null);
  assert.equal(specFromEvent({ key: 'Control', ctrlKey: true }), null);
  assert.equal(specFromEvent(null), null);
});

test('matches：平台修饰键归一（Ctrl 或 ⌘ 都算 Mod）', () => {
  assert.equal(matches({ key: 'k', ctrlKey: true }, 'Mod+K'), true);
  assert.equal(matches({ key: 'k', metaKey: true }, 'Mod+K'), true);
  assert.equal(matches({ key: 'K', ctrlKey: true, shiftKey: true }, 'Mod+K'), false);
  assert.equal(matches({ key: 'F5' }, 'F5'), true);
  assert.equal(matches({ key: 'k' }, 'Mod+K'), false);
  assert.equal(matches({ key: 'k', ctrlKey: true }, 'bogus'), false);
});

test('resolveShortcuts：默认值兜底，非法覆盖被忽略', () => {
  const map = resolveShortcuts({ palette: 'Alt+P', newRun: 'N', refresh: '' });
  assert.equal(map.palette, 'Alt+P');
  assert.equal(map.newRun, 'Mod+N');
  assert.equal(map.refresh, 'F5');
  assert.deepEqual(Object.keys(resolveShortcuts(null)), actionDefs().map((a) => a.id));
});

test('shortcutConflicts：同键位两动作只报一次', () => {
  const conflicts = shortcutConflicts({ palette: 'Mod+K', newRun: 'Ctrl+k' });
  assert.equal(conflicts.length, 1);
  assert.deepEqual(conflicts[0], { a: 'palette', b: 'newRun', spec: 'Ctrl+k' });
  assert.deepEqual(shortcutConflicts({ palette: 'Alt+P' }), []);
});

test('displaySpec：Mod 在 Windows 上显示为 Ctrl', () => {
  assert.equal(displaySpec('Mod+Shift+K'), 'Ctrl+Shift+K');
  assert.equal(displaySpec('F5'), 'F5');
});
