/**
 * 供应商对话框：在线模型多选、多端点测速。
 */
import { $, h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { switchNav } from '../../app/router.js';

import { openEditProviderModal } from './forms.js';
import { getEffectiveProviderModels } from './models.js';
/* 在线模型多选弹窗：搜索 + 复选框 + 已添加标记，勾选后写入 settings.providerModels */
function showFetchedModelsModal(p, fetched) {
  const key = p.key || p.name;
  const existing = new Set(getEffectiveProviderModels(p));
  const selected = new Set();
  let q = '';
  const modal = $('#modal');
  const finish = () => { closeModal(); setTimeout(() => modal.classList.remove('msw-modal'), 220); };

  const search = h('input', { class: 'msw-search', type: 'text', placeholder: '筛选模型 ID / 归属…' });
  const listEl = h('div', { class: 'fm-list' });
  const addBtn = h('button', { class: 'btn btn-primary', type: 'button', disabled: true }, '添加选中（0）');

  const syncAdd = () => {
    const n = [...selected].filter(id => !existing.has(id)).length;
    addBtn.textContent = `添加选中（${n}）`;
    addBtn.disabled = n === 0;
  };

  function renderList() {
    const query = q.trim().toLowerCase();
    listEl.innerHTML = '';
    const items = fetched.filter(m => !query
      || m.id.toLowerCase().includes(query)
      || String(m.owned_by || '').toLowerCase().includes(query));
    if (!items.length) { listEl.appendChild(h('div', { class: 'msw-empty' }, '无匹配模型')); return; }
    for (const m of items) {
      const isAdded = existing.has(m.id);
      const cb = h('input', { type: 'checkbox', class: 'fm-check', disabled: isAdded });
      cb.checked = isAdded || selected.has(m.id);
      cb.addEventListener('change', () => {
        if (cb.checked) selected.add(m.id); else selected.delete(m.id);
        syncAdd();
      });
      listEl.appendChild(h('label', { class: 'fm-row' + (isAdded ? ' is-added' : '') },
        cb,
        h('span', { class: 'fm-id' },
          h('span', { class: 'fm-id-text' }, m.id),
          m.owned_by ? h('span', { class: 'fm-owned' }, m.owned_by) : null),
        isAdded ? h('span', { class: 'fm-added-tag' }, '已添加') : null));
    }
  }

  let tmr = null;
  search.addEventListener('input', (e) => {
    clearTimeout(tmr);
    const v = e.target.value;
    tmr = setTimeout(() => { q = v; renderList(); }, 120);
  });

  addBtn.addEventListener('click', async () => {
    const ids = [...selected].filter(id => !existing.has(id));
    if (!ids.length) return;
    if (!S.settings) S.settings = {};
    if (!S.settings.providerModels) S.settings.providerModels = {};
    const previous = S.settings.providerModels[key];
    const nextModels = [...new Set([...(previous || []), ...ids])];
    S.settings.providerModels[key] = nextModels;
    // 只回写变更字段：整份 S.settings 是启动时的快照，会把主进程期间写入的
    // recentGoals / theme 等覆盖掉（lost update）。
    try {
      const saved = await window.studio.setSettings({ providerModels: S.settings.providerModels });
      if (!saved || !saved.providerModels) throw new Error('主进程未返回已保存的设置');
      S.settings.providerModels = saved.providerModels;
    } catch (e) {
      if (previous == null) delete S.settings.providerModels[key];
      else S.settings.providerModels[key] = previous;
      toast('error', '保存失败', String(e.message || e));
      return;
    }
    toast('success', '已添加模型', `${p.label || key}：新增 ${ids.length} 个`);
    finish();
    switchNav('providers');
  });

  showModal(h('div', { class: 'msw-wrap' },
    h('div', { class: 'msw-title-row' },
      h('h3', { class: 'modal-title', style: 'margin:0' }, `${p.label || key} · 在线可用模型（${fetched.length}）`),
      h('span', { class: 'msw-current' }, p.base_url || '')),
    search,
    listEl,
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: finish }, '关闭'),
      addBtn)));
  modal.classList.add('msw-modal');
  renderList(); syncAdd();
  setTimeout(() => search.focus(), 50);
}

/* ============================================================
   多端点测速（借鉴 cc-switch EndpointSpeedTest）
   并行测各端点 GET /models 延迟，按速度排序，可"采用"生成 base_url 覆盖
   ============================================================ */
function openEndpointSpeedTest(p) {
  if (!p.base_url) { toast('warning', '无法测速', '该供应商无 HTTP 端点'); return; }

  // 种子端点：当前 base_url + 预设候选（字符串数组或 {url} 数组）
  const seed = [];
  if (p.base_url) seed.push(p.base_url);
  (Array.isArray(p.endpoint_candidates) ? p.endpoint_candidates : []).forEach(c => {
    const u = typeof c === 'string' ? c : (c && c.url);
    if (u && !seed.includes(u)) seed.push(u);
  });
  let urls = [...seed];
  const resultMap = new Map(); // baseUrl -> result
  let testing = false;

  const modal = $('#modal');
  const finish = () => { closeModal(); setTimeout(() => modal.classList.remove('msw-modal'), 220); };

  const input = h('input', {
    class: 'msw-custom-input', type: 'text',
    placeholder: '添加自定义端点 Base URL，如 https://api.openai.com/v1',
  });
  const addBtn = h('button', { class: 'btn', type: 'button' }, '添加');
  const testAllBtn = h('button', { class: 'btn btn-primary', type: 'button' }, '⚡ 开始测速');
  const listEl = h('div', { class: 'ep-results' });

  function renderList() {
    listEl.innerHTML = '';
    const anyOk = [...resultMap.values()].some(x => x.ok);
    const sorted = [...urls].sort((a, b) => {
      const ra = resultMap.get(a), rb = resultMap.get(b);
      const la = ra && ra.ok ? ra.latency_ms : Infinity;
      const lb = rb && rb.ok ? rb.latency_ms : Infinity;
      return la - lb;
    });
    if (!sorted.length) { listEl.appendChild(h('div', { class: 'msw-empty' }, '请添加端点')); return; }
    sorted.forEach((u, idx) => {
      const r = resultMap.get(u);
      let latEl;
      if (testing && !r) latEl = h('span', { class: 'ep-lat fail' }, '测试中…');
      else if (!r) latEl = h('span', { class: 'ep-lat fail' }, '—');
      else if (!r.ok) latEl = h('span', { class: 'ep-lat fail', title: r.error || '' }, '失败');
      else {
        const cls = r.latency_ms < 300 ? 'good' : r.latency_ms < 800 ? 'warn' : 'bad';
        latEl = h('span', { class: 'ep-lat ' + cls }, r.latency_ms + 'ms');
      }
      const isBest = anyOk && r && r.ok && idx === 0;
      const dotStyle = r ? ('background:' + (r.ok ? 'var(--success-text)' : 'var(--error-text)')) : '';
      const label = u + (u === p.base_url ? ' （当前）' : '') + (isBest ? '  ⚡ 最快' : '');
      const row = h('div', {
        class: 'ep-row' + (isBest ? ' best' : ''),
        title: r && r.status ? ('HTTP ' + r.status + (r.error ? ' ' + r.error : '')) : '',
      },
        h('span', { class: 'ep-dot', style: dotStyle }),
        h('span', { class: 'ep-url' }, label),
        latEl,
        h('button', {
          class: 'ep-pick', type: 'button', title: '用该端点生成 base_url 覆盖 YAML',
          onclick: () => { finish(); openEditProviderModal({ ...p, base_url: u }); },
        }, '采用'));
      if (!seed.includes(u)) {
        row.appendChild(h('button', {
          class: 'ep-pick', type: 'button',
          // C-7：测速期间禁用移除，配合快照回填，避免下标错位
          disabled: testing,
          onclick: () => { urls = urls.filter(x => x !== u); resultMap.delete(u); renderList(); },
        }, '移除'));
      }
      listEl.appendChild(row);
    });
  }

  function addUrl() {
    const raw0 = input.value.trim();
    if (!raw0) return;
    const u = raw0.replace(/\/+$/, '');
    let parsed;
    try { parsed = new URL(u); } catch { toast('error', 'URL 无效', '请输入合法的 http(s) 地址'); return; }
    if (!/^https?:$/.test(parsed.protocol)) { toast('error', '协议不支持', '仅支持 http/https'); return; }
    if (urls.includes(u)) { toast('warning', '端点已存在', u); return; }
    urls.push(u); input.value = ''; renderList();
  }
  addBtn.addEventListener('click', addUrl);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addUrl(); } });

  testAllBtn.addEventListener('click', async () => {
    if (!urls.length || testing) return;
    // C-7：固定快照。请求在途时用户仍可能增删端点，若按下标回填可变的 urls，
    // 「最快」判定与「采用」会落到另一个端点上。测速期间同时禁用增删。
    const snapshot = [...urls];
    testing = true; resultMap.clear();
    addBtn.disabled = true;
    renderList();
    testAllBtn.disabled = true; testAllBtn.textContent = '测速中…';
    try {
      // 测 GET /models；结果保序，按快照下标映射回 base URL
      const targets = snapshot.map(u => {
        const b = u.replace(/\/+$/, '');
        return /\/models$/.test(b) ? b : b + '/models';
      });
      const r = await window.studio.providerTestEndpoints({ urls: targets });
      if (r.ok) r.results.forEach((x, i) => { if (snapshot[i]) resultMap.set(snapshot[i], x); });
      else toast('error', '测速失败', r.error || '');
    } catch (e) {
      toast('error', '测速失败', String(e && e.message || e));
    } finally {
      testing = false; addBtn.disabled = false;
      testAllBtn.disabled = false; testAllBtn.textContent = '⚡ 开始测速'; renderList();
    }
  });

  showModal(h('div', { class: 'msw-wrap' },
    h('div', { class: 'msw-title-row' },
      h('h3', { class: 'modal-title', style: 'margin:0' }, `端点测速 · ${p.label || p.name}`)),
    h('div', { style: 'font-size:11px;color:var(--text-sub);margin-bottom:8px;line-height:1.6' },
      '并行测试各端点 GET /models 延迟并按速度排序（绿 <300ms / 黄 <800ms / 红 ≥800ms）；点击"采用"生成 base_url 覆盖片段。'),
    h('div', { class: 'msw-custom' }, input, addBtn, testAllBtn),
    listEl,
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', type: 'button', onclick: finish }, '关闭'))));
  modal.classList.add('msw-modal');
  renderList();
  setTimeout(() => input.focus(), 50);
}

/* 构建添加/编辑弹窗共用的表单字段；返回控件引用与 genYaml。
   mode: 'custom' → models.custom_providers.<key>；'override' → models.providers.<key>（内置覆盖）
   initial.keyConfigured: 当前密钥是否已配置（编辑弹窗用于展示状态）
   initial.onKeySaved: 保存 Key 成功后的回调（刷新卡片状态点） */

export { openEndpointSpeedTest, showFetchedModelsModal };
