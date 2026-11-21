/**
 * 供应商页：先分「大模型供应商 / Agent 智能体」，再按分类与名称排序。
 *
 * 三条边界（对齐后端 PROVIDER_PRESETS）：
 * - 只有大模型供应商参与默认模型 / 模型切换；Agent 智能体是 CLI 执行引擎；
 * - mock 是离线测试替身，不属于供应商，页面不展示；
 * - 排序规则固定：大模型在前 → 国内/国际/聚合/本地 → 同组按名称，Agent 按名称。
 */
import { BUILTIN_PROVIDERS, findProvider, providerType } from '../../contracts/providers.js';

import { apiFetch } from '../../lib/api.js';
import { $, h } from '../../lib/dom.js';
import { sortProviders } from '../../lib/providers.js';
import { S } from '../../lib/store.js';

import { isStaleRender } from '../../ui/states.js';

import { switchNav } from '../../app/router.js';

import { buildProviderCard } from './card.js';
import { openAddProviderModal } from './forms.js';
import { openModelSwitcher } from './models.js';

const CAT_ORDER = ['national', 'international', 'aggregator', 'local', 'specialized'];
const CAT_LABELS = { national: '国内', international: '国际', aggregator: '聚合', local: '本地' };
const TYPE_ORDER = [
  { key: 'all', label: '全部' },
  { key: 'llm', label: '大模型' },
  { key: 'agent', label: 'Agent 智能体' },
];

function sectionMeta(type) {
  if (type === 'agent') {
    return {
      title: 'Agent 智能体',
      desc: '真实 CLI 执行引擎：自带工具循环与认证，不是模型 API；按编程 Agent Token 份额排序（2026-11-18 观测样本）。',
    };
  }
  return {
    title: '大模型供应商',
    desc: '提供 chat/completions 接口；按 OpenRouter 作者请求份额排序（2026-11-09 当周，公开代理指标）。',
  };
}

async function renderProvidersPage(container) {
  const token = S.navToken;
  container.appendChild(h('div', { class: 'page-head prov-head' },
    h('div', { style: 'display:flex;align-items:center;gap:10px' },
      h('h2', {}, '供应商与智能体'),
      h('span', { class: 'conn-indicator demo', id: 'prov-source-badge' }, '加载中…')),
    h('div', { style: 'display:flex;gap:8px' },
      h('button', { class: 'btn', type: 'button', onclick: () => openModelSwitcher() }, '切换模型'),
      h('button', { class: 'btn btn-primary', id: 'prov-add-preset', type: 'button' }, '从预设添加'))));

  S.provFilter = S.provFilter || {};
  const filterState = S.provFilter;
  filterState.q = filterState.q || '';
  filterState.cat = filterState.cat || 'all';
  filterState.type = filterState.type || 'all';

  const typeBar = h('div', { class: 'prov-type-bar', id: 'prov-type-bar', role: 'tablist', 'aria-label': '条目类型' });
  const catBar = h('div', { class: 'prov-cat-bar', id: 'prov-cat-bar', role: 'group', 'aria-label': '大模型分类' });
  const sortHint = h('span', { class: 'prov-sort-hint', id: 'prov-sort-hint' });
  const toolbar = h('div', { class: 'prov-toolbar prov-toolbar-v2' },
    h('div', { class: 'prov-toolbar-row' },
      h('input', {
        id: 'prov-search', class: 'prov-search', type: 'text',
        placeholder: '搜索供应商（名称 / 别名）…', value: filterState.q,
      }),
      typeBar,
      h('span', { class: 'prov-count', id: 'prov-count' })),
    h('div', { class: 'prov-toolbar-sub' }, catBar, sortHint));
  container.appendChild(toolbar);

  const listEl = h('div', { class: 'prov-list', id: 'prov-list' });
  container.appendChild(listEl);
  container.appendChild(h('p', { class: 'prov-foot-note' },
    'Mock 是离线测试替身，不属于供应商，已从列表隐藏；需要离线演练时在「新建运行」里勾选 Mock。'));

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
      category: p.category, family: p.family,
      search_aliases: p.search_aliases || [],
      api_key_url: p.api_key_url, endpoint_candidates: p.endpoint_candidates,
      command: p.command, agentic: p.agentic, type: p.type,
    }));
    source = base ? 'watch' : 'demo';
  }
  badge.className = 'conn-indicator ' + (source === 'api' ? 'live' : source === 'watch' ? 'watch' : 'demo');
  badge.textContent = source === 'api' ? '后端 API' : source === 'watch' ? '文件监听' : '演示模式';

  // 补齐元数据 + 判定类型；internal（mock）直接丢掉，不进入任何列表
  const enriched = (await Promise.all(providers.map(async (p) => {
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
    const preset = findProvider(p.name);
    const family = p.family || (preset && preset.family) || 'openai_compat';
    const type = preset ? preset.type : providerType({ key: p.name, family });
    return {
      ...p,
      _status: status,
      _builtin: p.kind !== 'custom',
      type,
      family,
      label: p.label || (preset && preset.label) || p.name,
      category: p.category || (preset && preset.category) || 'specialized',
      // 有内置预设时以「已核对的前端目录」为准；后端静态预设里的旧模型不再覆盖它
      suggested_models: (preset && preset.suggested_models && preset.suggested_models.length)
        ? preset.suggested_models
        : (Array.isArray(p.suggested_models) ? p.suggested_models : []),
      search_aliases: p.search_aliases || (preset && preset.search_aliases) || [],
      api_key_url: p.api_key_url != null ? p.api_key_url : (preset && preset.api_key_url) || null,
      endpoint_candidates: p.endpoint_candidates != null ? p.endpoint_candidates : (preset && preset.endpoint_candidates) || null,
      command: p.command || (preset && preset.command) || null,
      agentic: p.agentic != null ? p.agentic : !!(preset && preset.agentic),
      // 调用量是前端展示元数据，API 不返回；从内置目录补齐
      usage: p.usage || (preset && preset.usage) || null,
    };
  }))).filter(p => p.type !== 'internal');

  if (isStaleRender(token, container)) return;

  // ── 类型 tabs + 分类 chips ──
  const typeBtns = {};
  for (const t of TYPE_ORDER) {
    const btn = h('button', {
      class: 'prov-type-btn', type: 'button', role: 'tab', 'data-type': t.key,
      'aria-selected': 'false',
      onclick: () => { filterState.type = t.key; renderTypeBar(); renderCatBar(); renderProvList(); },
    }, h('span', {}, t.label), h('span', { class: 'prov-type-n' }, ''));
    typeBtns[t.key] = btn;
    typeBar.appendChild(btn);
  }
  for (const cat of ['all', ...CAT_ORDER.slice(0, 4)]) {
    catBar.appendChild(h('button', {
      class: 'prov-cat-btn', type: 'button', 'data-cat': cat,
      onclick: (ev) => {
        filterState.cat = cat;
        catBar.querySelectorAll('.prov-cat-btn').forEach(x => x.classList.remove('active'));
        ev.currentTarget.classList.add('active');
        renderProvList();
      },
    }, cat === 'all' ? '全部分类' : CAT_LABELS[cat]));
  }

  let searchTimer = null;
  $('#prov-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { filterState.q = e.target.value; renderProvList(); }, 200);
  });

  function renderTypeBar() {
    for (const t of TYPE_ORDER) {
      const btn = typeBtns[t.key];
      const n = t.key === 'all' ? enriched.length : enriched.filter(p => p.type === t.key).length;
      btn.classList.toggle('active', filterState.type === t.key);
      btn.setAttribute('aria-selected', filterState.type === t.key ? 'true' : 'false');
      btn.querySelector('.prov-type-n').textContent = String(n);
    }
  }

  function renderCatBar() {
    const agentOnly = filterState.type === 'agent';
    catBar.classList.toggle('is-disabled', agentOnly);
    catBar.querySelectorAll('.prov-cat-btn').forEach((b) => {
      b.disabled = agentOnly;
      b.classList.toggle('active', !agentOnly && b.dataset.cat === filterState.cat);
    });
    if (filterState.type === 'agent') {
      sortHint.textContent = '排序：按编程 Agent Token 份额（2026-11-18 观测样本）';
    } else if (filterState.type === 'llm') {
      sortHint.textContent = '排序：按 OpenRouter 请求份额（2026-11-09 周）→ 未上榜按分类/常用/名称';
    } else {
      sortHint.textContent = '排序：大模型（按 OpenRouter 请求份额）→ Agent 智能体（按 Token 份额）';
    }
  }

  function applyFilter() {
    const q = (filterState.q || '').trim().toLowerCase();
    const cat = filterState.cat || 'all';
    return enriched.filter((p) => {
      if (filterState.type !== 'all' && p.type !== filterState.type) return false;
      // 分类只筛大模型；Agent 没有国内/聚合这类业务分类
      if (filterState.type !== 'agent' && cat !== 'all' && p.category !== cat) return false;
      if (!q) return true;
      const hay = [p.name, p.label, p.key, ...(p.search_aliases || [])]
        .filter(Boolean).join(' ').toLowerCase();
      return hay.includes(q);
    });
  }

  function renderSection(type, items) {
    const meta = sectionMeta(type);
    const list = h('div', { class: 'prov-section-list' });
    for (const p of items) list.appendChild(buildProviderCard(p, testDebounce));
    return h('section', { class: 'prov-section', 'data-kind': type },
      h('div', { class: 'prov-section-head' },
        h('h3', {}, meta.title),
        h('span', { class: 'prov-section-n' }, String(items.length)),
        h('span', { class: 'prov-section-desc' }, meta.desc)),
      list);
  }

  function renderProvList() {
    const list = $('#prov-list');
    if (!list) return;
    const filtered = sortProviders(applyFilter());
    list.innerHTML = '';
    const countEl = $('#prov-count');
    const filtering = !!(filterState.q || filterState.cat !== 'all' || filterState.type !== 'all');
    if (countEl) {
      countEl.textContent = filtering
        ? `${filtered.length} 个匹配 / 共 ${enriched.length} 个条目`
        : ''; // 默认视图的数量已由类型 tabs 给出，不重复一次
    }
    if (!filtered.length) {
      list.appendChild(h('div', { class: 'prov-empty' },
        h('h3', {}, '无匹配条目'),
        h('p', {}, '尝试调整搜索关键词、类型或分类筛选。')));
      return;
    }
    if (filterState.type === 'all') {
      const llm = filtered.filter(p => p.type === 'llm');
      const agents = filtered.filter(p => p.type === 'agent');
      if (llm.length) list.appendChild(renderSection('llm', llm));
      if (agents.length) list.appendChild(renderSection('agent', agents));
    } else {
      list.appendChild(renderSection(filterState.type, filtered));
    }
  }

  listEl.innerHTML = '';
  if (!enriched.length) {
    listEl.appendChild(h('div', { class: 'prov-empty' },
      h('h3', {}, '无法加载供应商列表'),
      h('p', {}, '后端 API 不可达，且本地常量不可用。'),
      h('button', { class: 'btn btn-primary', onclick: () => switchNav('settings') }, '到设置页配置后端地址')));
    return;
  }
  renderTypeBar();
  renderCatBar();
  renderProvList();
  $('#prov-add-preset').addEventListener('click', openAddProviderModal);
}

export { renderProvidersPage, sectionMeta };
