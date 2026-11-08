/**
 * 模型切换器：按供应商分组的别名选择 + 应用到当前 run。
 */
import { PROV_CATEGORY_LABELS } from '../../contracts/enums.js';
import { BUILTIN_PROVIDERS } from '../../contracts/providers.js';

import { $, h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { pushEvent } from '../../app/mock.js';
import { renderChrome } from '../../app/shell.js';

import { renderDashboard } from '../dashboard/index.js';
function getEffectiveProviderModels(p) {
  const key = p.key || p.name;
  const extra = (S.settings && S.settings.providerModels && S.settings.providerModels[key]) || [];
  const seen = new Set(); const out = [];
  for (const m of [...(p.suggested_models || []), ...extra]) {
    if (m && !seen.has(m)) { seen.add(m); out.push(m); }
  }
  return out;
}

/* 模型切换弹窗：按供应商分组（可折叠）+ 搜索 + 分类筛选（借鉴 cc-switch 预设选择器） */
function openModelSwitcher() {
  const current = S.dash.run.model || (S.settings && S.settings.defaultModel) || '';
  const currentProvider = current.includes(':') ? current.split(':')[0] : '';
  const ui = { q: '', cat: 'all' };
  // 默认折叠除当前供应商外的全部分组
  const collapsed = new Set(BUILTIN_PROVIDERS.filter(p => p.key !== currentProvider).map(p => p.key));

  const modal = $('#modal');
  const finish = () => { closeModal(); setTimeout(() => modal.classList.remove('msw-modal'), 220); };
  const pick = (alias) => { applyModel(alias); finish(); };

  const catOrder = [
    ['all', '全部'], ['international', '国际'], ['national', '国内'],
    ['aggregator', '聚合'], ['local', '本地'], ['specialized', '专用'],
  ];

  const searchInput = h('input', {
    class: 'msw-search', type: 'text', value: ui.q,
    placeholder: '搜索模型或供应商，如 gpt、claude、kimi、智谱…',
  });
  const catBar = h('div', { class: 'msw-cats' });
  const groupsEl = h('div', { class: 'msw-groups' });
  const emptyEl = h('div', { class: 'msw-empty hidden' }, '无匹配模型');

  const providerMatches = (p, q) => {
    if (!q) return true;
    const hay = [p.label, p.key, ...(p.search_aliases || [])].filter(Boolean).join(' ').toLowerCase();
    return hay.includes(q);
  };

  function renderCats() {
    catBar.innerHTML = '';
    for (const [val, label] of catOrder) {
      catBar.appendChild(h('button', {
        class: 'prov-cat-btn msw-cat' + (ui.cat === val ? ' active' : ''), type: 'button',
        onclick: () => { ui.cat = val; renderCats(); renderGroups(); },
      }, label));
    }
  }

  function renderGroups() {
    const q = ui.q.trim().toLowerCase();
    groupsEl.innerHTML = '';
    let anyVisible = false;
    for (const p of BUILTIN_PROVIDERS) {
      if (ui.cat !== 'all' && p.category !== ui.cat) continue;
      const allModels = getEffectiveProviderModels(p);
      if (!allModels.length) continue; // 无模型的供应商（如 cli）不显示
      const provHit = providerMatches(p, q);
      const models = (q && !provHit) ? allModels.filter(m => m.toLowerCase().includes(q)) : allModels;
      if (q && !provHit && !models.length) continue;
      anyVisible = true;

      const isCollapsed = q ? false : collapsed.has(p.key);
      const catBadge = PROV_CATEGORY_LABELS[p.category]
        ? h('span', { class: 'prov-cat prov-cat-' + p.category }, PROV_CATEGORY_LABELS[p.category]) : null;
      const head = h('button', {
        class: 'msw-group-head', type: 'button',
        onclick: () => { collapsed.has(p.key) ? collapsed.delete(p.key) : collapsed.add(p.key); renderGroups(); },
      },
        h('span', { class: 'msw-avatar' }, (p.label || p.key).trim().charAt(0).toUpperCase()),
        h('span', { class: 'msw-group-name' }, p.label),
        catBadge,
        h('span', { class: 'msw-group-count' }, String(allModels.length)),
        h('span', { class: 'msw-chevron' + (isCollapsed ? '' : ' open') }, '▾'));

      const body = h('div', { class: 'msw-group-body' + (isCollapsed ? ' hidden' : '') });
      for (const m of models) {
        const alias = `${p.key}:${m}`;
        const isCurrent = alias === current;
        body.appendChild(h('button', {
          class: 'msw-model' + (isCurrent ? ' active' : ''), type: 'button', title: alias,
          onclick: () => pick(alias),
        }, (isCurrent ? '✓ ' : '') + m));
      }
      groupsEl.appendChild(h('div', { class: 'msw-group' }, head, body));
    }
    emptyEl.classList.toggle('hidden', anyVisible);
  }

  let tmr = null;
  searchInput.addEventListener('input', (e) => {
    clearTimeout(tmr);
    const v = e.target.value;
    tmr = setTimeout(() => { ui.q = v; renderGroups(); }, 150);
  });

  const customInput = h('input', {
    class: 'msw-custom-input', type: 'text',
    placeholder: '自定义别名 provider:model，如 openai:gpt-6-astra',
  });
  customInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { const v = customInput.value.trim(); if (v) pick(v); }
  });
  const customApply = h('button', {
    class: 'btn btn-primary', type: 'button',
    onclick: () => { const v = customInput.value.trim(); if (v) pick(v); },
  }, '应用');

  showModal(h('div', { class: 'msw-wrap' },
    h('div', { class: 'msw-title-row' },
      h('h3', { class: 'modal-title', style: 'margin:0' }, '切换模型'),
      current ? h('span', { class: 'msw-current', title: current }, '当前：' + current) : null),
    searchInput,
    catBar,
    groupsEl,
    emptyEl,
    h('div', { class: 'msw-custom' }, customInput, customApply),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', type: 'button', onclick: finish }, '取消'))));
  modal.classList.add('msw-modal');
  renderCats();
  renderGroups();
  setTimeout(() => searchInput.focus(), 50);
}

async function applyModel(alias) {
  const prev = (S.dash.run && S.dash.run.model) || (S.settings && S.settings.defaultModel) || '';
  if (S.realMode && S.dash.run && S.dash.run.dir) {
    try {
      await window.studio.switchModel(S.dash.run.dir, alias);
      S.dash.run.model = alias;
      if (S.settings) { S.settings.defaultModel = alias; window.studio.setSettings({ defaultModel: alias }).catch(() => {}); }
      pushEvent('llm_model_switched', { from: prev, to: alias });
      renderChrome(); renderDashboard();
      toast('success', '模型已切换', alias);
    } catch (e) {
      toast('error', '切换失败', String(e.message || e));
    }
  } else {
    S.dash.run.model = alias;
    if (S.settings) { S.settings.defaultModel = alias; window.studio.setSettings({ defaultModel: alias }).catch(() => {}); }
    pushEvent('llm_model_switched', { from: prev, to: alias });
    renderChrome(); renderDashboard();
    toast('success', '模型已切换', alias);
  }
}

/* ============================================================
   任务图全屏页（分层列表视图）
   ============================================================ */

export { applyModel, getEffectiveProviderModels, openModelSwitcher };
