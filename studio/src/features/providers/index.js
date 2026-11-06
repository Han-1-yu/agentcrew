/**
 * 供应商页：搜索 + 分类筛选 + 卡片列表 + 添加入口。
 */
import { BUILTIN_PROVIDERS, findProvider } from '../../contracts/providers.js';

import { apiFetch } from '../../lib/api.js';
import { $, h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { isStaleRender } from '../../ui/states.js';

import { ev } from '../../app/mock.js';
import { switchNav } from '../../app/router.js';

import { buildProviderCard } from './card.js';
import { openAddProviderModal } from './forms.js';
import { openModelSwitcher } from './models.js';
async function renderProvidersPage(container) {
  const token = S.navToken;
  container.appendChild(h('div', { class: 'page-head prov-head' },
    h('div', { style: 'display:flex;align-items:center;gap:10px' },
      h('h2', {}, '供应商管理'),
      h('span', { class: 'conn-indicator demo', id: 'prov-source-badge' }, '加载中…')),
    h('div', { style: 'display:flex;gap:8px' },
      h('button', { class: 'btn', type: 'button', onclick: () => openModelSwitcher() }, '切换模型'),
      h('button', { class: 'btn btn-primary', id: 'prov-add-preset', type: 'button' }, '从预设添加'))));

  // 搜索 + 分类筛选工具条
  S.provFilter = S.provFilter || { q: '', cat: 'all' };
  const toolBar = h('div', { class: 'prov-toolbar' },
    h('input', {
      id: 'prov-search', class: 'prov-search', type: 'text',
      placeholder: '搜索供应商（名称 / 别名）…', value: S.provFilter.q || '',
    }),
    h('div', { class: 'prov-cat-bar', id: 'prov-cat-bar' }),
    h('span', { class: 'prov-count', id: 'prov-count' }));
  container.appendChild(toolBar);

  const listEl = h('div', { class: 'prov-list', id: 'prov-list' });
  container.appendChild(listEl);

  // 骨架屏
  const skeleton = h('div', { class: 'prov-skeleton' });
  for (let i = 0; i < 4; i++) skeleton.appendChild(h('div', { class: 'skeleton prov-skel-card' }));
  listEl.appendChild(skeleton);

  const badge = $('#prov-source-badge');
  const testDebounce = new Map();

  // 数据源三级降级：apiBaseUrl → 常量+keyStatus → demo
  let providers = null;
  let source = 'demo';
  const base = (S.settings && S.settings.apiBaseUrl) || '';
  if (base) {
    try {
      const data = await apiFetch('/providers', { timeoutMs: 5000 });
      const list = Array.isArray(data) ? data : (data && data.providers) || [];
      if (list.length) { providers = list; source = 'api'; }
    } catch (e) { /* 回落常量 */ }
  }
  if (!providers) {
    providers = BUILTIN_PROVIDERS.map(p => ({
      name: p.key, label: p.label, kind: 'builtin',
      base_url: p.base_url, api_key_env: p.api_key_env || '',
      suggested_models: p.suggested_models || [],
      category: p.category, search_aliases: p.search_aliases || [],
      api_key_url: p.api_key_url, endpoint_candidates: p.endpoint_candidates,
    }));
    source = base ? 'watch' : 'demo';
  }
  badge.className = 'conn-indicator ' + (source === 'api' ? 'live' : source === 'watch' ? 'watch' : 'demo');
  badge.textContent = source === 'api' ? '后端 API' : source === 'watch' ? '文件监听' : '演示模式';

  // 内置/常量来源时，对每个有 api_key_env 的 provider 查询配置状态（只回布尔）
  const enriched = await Promise.all(providers.map(async (p) => {
    const envName = p.api_key_env || '';
    let status = 'none';
    if (envName && window.studio.providerKeyStatus) {
      try {
        const r = await window.studio.providerKeyStatus(envName);
        status = r.configured ? 'configured' : 'missing';
      } catch { status = 'missing'; }
    } else if (p.key_configured != null) {
      status = p.key_configured ? 'configured' : 'missing';
    }
    // 元数据回填（API 已带则保留；缺失时从内置常量补齐）
    const preset = findProvider(p.name);
    return {
      ...p, _status: status, _builtin: p.kind !== 'custom',
      label: p.label || (preset && preset.label) || p.name,
      category: p.category || (preset && preset.category) || 'specialized',
      family: p.family || (preset && preset.family) || 'openai_compat',
      suggested_models: Array.isArray(p.suggested_models) && p.suggested_models.length
        ? p.suggested_models : (preset && preset.suggested_models) || [],
      search_aliases: p.search_aliases || (preset && preset.search_aliases) || [],
      api_key_url: p.api_key_url != null ? p.api_key_url : (preset && preset.api_key_url) || null,
      endpoint_candidates: p.endpoint_candidates != null ? p.endpoint_candidates : (preset && preset.endpoint_candidates) || null,
    };
  }));

  // 上面两次 await 期间用户可能已经切页：占位区被清空，全局 ID 选择器会拿到
  // null（或更糟——写进新版页面造成重复按钮）。过期就直接放弃这次渲染。
  if (isStaleRender(token, container)) return;

  // ── 分类筛选按钮 ──
  const catBar = $('#prov-cat-bar');
  const catOrder = [
    ['all', '全部'], ['aggregator', '聚合'], ['national', '国内'],
    ['international', '国际'], ['local', '本地'], ['specialized', '专用'],
  ];
  const catBtns = {};
  for (const [val, label] of catOrder) {
    const b = h('button', {
      class: 'prov-cat-btn' + (S.provFilter.cat === val ? ' active' : ''),
      type: 'button', 'data-cat': val,
      onclick: (ev) => {
        S.provFilter.cat = val;
        catBar.querySelectorAll('.prov-cat-btn').forEach(x => x.classList.remove('active'));
        ev.currentTarget.classList.add('active');
        renderProvList();
      },
    }, label);
    catBtns[val] = b;
    catBar.appendChild(b);
  }

  // ── 搜索输入（200ms 防抖）──
  let searchTimer = null;
  $('#prov-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { S.provFilter.q = e.target.value; renderProvList(); }, 200);
  });

  // ── 列表渲染（搜索 + 分类 AND 过滤）──
  function applyFilter() {
    const q = (S.provFilter.q || '').trim().toLowerCase();
    const cat = S.provFilter.cat || 'all';
    return enriched.filter(p => {
      if (cat !== 'all' && p.category !== cat) return false;
      if (!q) return true;
      const hay = [p.name, p.label, p.key, ...(p.search_aliases || [])]
        .filter(Boolean).join(' ').toLowerCase();
      return hay.includes(q);
    });
  }
  function renderProvList() {
    const list = $('#prov-list');
    if (!list) return;
    const filtered = applyFilter();
    list.innerHTML = '';
    const countEl = $('#prov-count');
    if (countEl) {
      countEl.textContent = (S.provFilter.q || S.provFilter.cat !== 'all')
        ? `${filtered.length} 个匹配 / 共 ${enriched.length} 个供应商`
        : `${enriched.length} 个供应商`;
    }
    if (!filtered.length) {
      list.appendChild(h('div', { class: 'prov-empty' },
        h('h3', {}, '无匹配供应商'),
        h('p', {}, '尝试调整搜索关键词或分类筛选。')));
      return;
    }
    for (const p of filtered) list.appendChild(buildProviderCard(p, testDebounce));
  }

  listEl.innerHTML = '';
  if (!enriched.length) {
    listEl.appendChild(h('div', { class: 'prov-empty' },
      h('h3', {}, '无法加载供应商列表'),
      h('p', {}, '后端 API 不可达，且本地常量不可用。'),
      h('button', { class: 'btn btn-primary', onclick: () => switchNav('settings') }, '到设置页配置后端地址')));
    return;
  }
  renderProvList();
  $('#prov-add-preset').addEventListener('click', openAddProviderModal);
}

export { renderProvidersPage };
