/**
 * 命令框内的「快速切换模型」下拉（popover）。
 *
 * 定位：输入卡右下角那个模型名本身就是开关——点它在上方弹出短名单，选完即生效，
 * 不必先进「供应商」页或完整切换弹窗。
 *
 * 语义：命令框发起的是**下一次运行**，所以这里改的是 settings.defaultModel
 * （新建运行在没显式指定模型时读它），**不去改当前 run**——主进程的 model:switch
 * 在运行中会直接拒绝，而改一条已结束 run 的模型等于篡改历史。
 * 需要「把当前 run 换成这个模型」时走完整切换弹窗（菜单底部的「配置自定义模型」）。
 *
 * 交互契约：↑/↓ 移动、Enter 选中、Esc 关闭并把焦点还给开关、点击外部关闭；
 * 菜单打开时按秒刷新的 syncComposer 只更新开关文案，不重建列表（否则焦点会丢）。
 */
import { findProvider } from '../../contracts/providers.js';

import { h, s } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { openModelSwitcher } from '../providers/models.js';

import { QUICK_MODEL_ALIASES, metaOf } from './quick-models.js';

const MENU_ID = 'cmp-model-menu';
const TRIGGER_ID = 'cmp-model';

let menu = null;
let trigger = null;
let listEl = null;
let rows = [];
let focusIdx = -1;
let isOpen = false;
let onPicked = null;

/** 当前生效模型：命令框看的是「下一次运行」的默认模型，其次才是当前运行的模型。 */
function currentAlias() {
  return (S.settings && S.settings.defaultModel)
    || (S.dash && S.dash.run && S.dash.run.model) || '';
}

function providerOf(alias) {
  const i = alias.indexOf(':');
  return i < 0 ? '' : alias.slice(0, i);
}

function modelOf(alias) {
  const i = alias.indexOf(':');
  return i < 0 ? alias : alias.slice(i + 1);
}

function fmtWeight(w) {
  return (w == null || Number.isNaN(w)) ? '' : w.toFixed(2) + 'x';
}

function checkIcon() {
  return s('svg', {
    class: 'cmm-check', width: '14', height: '14', viewBox: '0 0 16 16', 'aria-hidden': 'true',
  }, s('path', {
    d: 'M3.2 8.5 6.2 11.5 12.8 4.9', fill: 'none', stroke: 'currentColor',
    'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }));
}

function providerIcon(alias) {
  const key = providerOf(alias);
  const prov = findProvider(key);
  const label = (prov && prov.label) || key || '?';
  return h('span', { class: 'cmm-ico', 'aria-hidden': 'true' }, label.trim().charAt(0).toUpperCase());
}

function buildRow(alias, current) {
  const meta = metaOf(alias);
  const selected = alias === current;
  const cost = fmtWeight(meta.costWeight);

  const row = h('button', {
    class: 'cmm-item' + (selected ? ' is-current' : ''),
    type: 'button',
    title: cost ? `${alias} · 相对成本 ${cost}（估算）` : alias,
  },
    providerIcon(alias),
    h('span', { class: 'cmm-name' }, modelOf(alias)),
    meta.badge ? h('span', { class: 'cmm-badge cmm-badge-' + (meta.badge.tone || 'gray') }, meta.badge.text) : null,
    h('span', { class: 'cmm-cost' }, cost),
    checkIcon());

  // h() 对 aria-* 只能落成属性名——role / aria-checked 必须显式 setAttribute。
  row.setAttribute('role', 'menuitemradio');
  row.setAttribute('aria-checked', selected ? 'true' : 'false');
  row.tabIndex = -1;
  row.dataset.alias = alias;
  row.addEventListener('click', () => pick(alias));
  return row;
}

function render(current) {
  const aliases = QUICK_MODEL_ALIASES.slice();
  // 当前模型不在短名单里（手输的自定义别名 / mock）时置顶，否则用户会以为没选中
  if (current && aliases.indexOf(current) < 0) aliases.unshift(current);

  listEl.textContent = '';
  rows = aliases.map((alias) => buildRow(alias, current));
  for (const row of rows) listEl.appendChild(row);
  focusIdx = -1;
  return Math.max(0, aliases.indexOf(current));
}

function focusItem(i) {
  if (!rows.length) return;
  focusIdx = (i + rows.length) % rows.length;
  rows.forEach((row, idx) => row.classList.toggle('is-focus', idx === focusIdx));
  rows[focusIdx].focus();
  if (rows[focusIdx].scrollIntoView) rows[focusIdx].scrollIntoView({ block: 'nearest' });
}

function openMenu() {
  if (isOpen || !menu) return;
  const current = currentAlias();
  const startIdx = render(current);
  menu.classList.remove('hidden');
  trigger.classList.add('open');
  trigger.setAttribute('aria-expanded', 'true');
  isOpen = true;
  // 等 popover 脱离 display:none 再聚焦，否则 focus() 在隐藏节点上无效
  requestAnimationFrame(() => { if (isOpen) focusItem(startIdx); });
}

function closeMenu(restoreFocus) {
  if (!isOpen) return;
  isOpen = false;
  focusIdx = -1;
  menu.classList.add('hidden');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
  rows.forEach((row) => row.classList.remove('is-focus'));
  if (restoreFocus && trigger) trigger.focus();
}

function toggleMenu() {
  if (isOpen) closeMenu(true);
  else openMenu();
}

/** 选中：先收起菜单（防连点重复切），保存失败要回滚，别让界面显示一个没生效的值。 */
async function pick(alias) {
  const prev = currentAlias();
  closeMenu(false);
  if (alias === prev) { if (trigger) trigger.focus(); return; }

  if (!S.settings) S.settings = {};
  const settings = S.settings;
  settings.defaultModel = alias;
  if (typeof onPicked === 'function') onPicked();
  try {
    await window.studio.setSettings({ defaultModel: alias });
    toast('success', '模型已切换', alias + ' · 下次运行生效');
  } catch (e) {
    settings.defaultModel = prev;
    if (typeof onPicked === 'function') onPicked();
    toast('error', '保存失败', String((e && e.message) || e));
  } finally {
    if (trigger) trigger.focus();
  }
}

function onMenuKeydown(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); focusItem(focusIdx + 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); focusItem(focusIdx - 1); }
  else if (e.key === 'Home') { e.preventDefault(); focusItem(0); }
  else if (e.key === 'End') { e.preventDefault(); focusItem(rows.length - 1); }
  else if (e.key === 'Escape') {
    // 阻断冒泡：全局 Esc 还会去关详情面板/模态，这里只收自己的菜单
    e.preventDefault();
    e.stopPropagation();
    closeMenu(true);
  } else if (e.key === 'Tab') { closeMenu(false); }
}

function openFullSwitcher() {
  closeMenu(false);
  openModelSwitcher();
}

function initModelMenu(opts = {}) {
  menu = document.getElementById(MENU_ID);
  trigger = document.getElementById(TRIGGER_ID);
  if (!menu || !trigger) return;
  onPicked = opts.onPicked;

  listEl = h('div', { class: 'cmm-list' });
  listEl.setAttribute('role', 'none');
  menu.appendChild(listEl);
  const configBtn = h('button', { class: 'cmm-config', type: 'button', onclick: openFullSwitcher },
    s('svg', {
      width: '14', height: '14', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': 'true',
    }, s('path', {
      d: 'M10.9 2.9a1.3 1.3 0 0 1 1.8 0l.4.4a1.3 1.3 0 0 1 0 1.8L6 12.2l-2.6.6.6-2.6 6.9-7.3Z',
      stroke: 'currentColor', 'stroke-width': '1.2', 'stroke-linejoin': 'round',
    })),
    h('span', {}, '配置自定义模型'));
  configBtn.setAttribute('role', 'menuitem');
  const foot = h('div', { class: 'cmm-foot' }, configBtn);
  foot.setAttribute('role', 'none');
  menu.appendChild(foot);

  trigger.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(); });
  trigger.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); openMenu(); }
    else if (e.key === 'Escape') closeMenu(false);
  });
  menu.addEventListener('keydown', onMenuKeydown);
  document.addEventListener('click', (e) => {
    if (!isOpen) return;
    if (menu.contains(e.target) || trigger.contains(e.target)) return;
    closeMenu(false);
  });
  window.addEventListener('resize', () => closeMenu(false));
  window.addEventListener('studio:nav', () => closeMenu(false));
}

export { initModelMenu };
