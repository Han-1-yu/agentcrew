/**
 * 供应商卡片：状态徽标、端点、Key 状态、按需拉取在线模型。
 */
import { PROV_CATEGORY_LABELS } from '../../contracts/enums.js';
import { PROVIDER_TYPE_LABELS } from '../../contracts/providers.js';

import { $, $$, h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { openEditProviderModal } from './forms.js';
import { openEndpointSpeedTest, showFetchedModelsModal } from './modals.js';
import { getEffectiveProviderModels } from './models.js';
function buildProviderCard(p, testDebounce) {
  const initial = (p.label || p.name || p.key || '?').trim().charAt(0).toUpperCase();
  const needsHttp = !!p.base_url;
  const isAgent = p.type === 'agent';
  const models = getEffectiveProviderModels(p);

  const statusDot = p._status === 'configured'
    ? h('span', { class: 'prov-dot ok', title: '已配置密钥' })
    : p._status === 'missing'
      ? h('span', { class: 'prov-dot off', title: '未检测到环境变量 ' + (p.api_key_env || '') })
      : h('span', { class: 'prov-dot none', title: '无需密钥' });

  // 类型徽标：先分清「大模型」与「Agent 智能体」，再谈分类
  const typeLabel = PROVIDER_TYPE_LABELS[p.type] || null;
  const typeBadge = typeLabel
    ? h('span', {
        class: 'prov-type-tag prov-type-' + p.type,
        title: isAgent ? 'CLI 型执行引擎，不是模型 API' : '大模型推理 API',
      }, typeLabel)
    : null;
  // 调用量徽标：数字来自公开代理指标，tooltip 写清口径与日期，避免被当成官方全量数据
  const u = p.usage;
  const usageBadge = u
    ? h('span', {
        class: 'prov-usage-tag',
        title: `${u.metric}：${u.share}%（${u.asOf}）`
          + (u.spendShare != null ? `；企业 API 支出份额 ${u.spendShare}%（Menlo 2026）` : ''),
      }, `#${u.rank} ${u.share}%`)
    : null;

  // 分类徽标：Agent 的类型徽标已经写明「Agent 智能体」，不再叠一个分类徽标
  const catLabel = isAgent ? null : (PROV_CATEGORY_LABELS[p.category] || null);
  const catBadge = catLabel
    ? h('span', { class: 'prov-cat prov-cat-' + p.category, title: '分类：' + catLabel }, catLabel)
    : null;

  const resultBox = h('div', { class: 'prov-test-result' });

  const testBtn = h('button', {
    class: 'btn btn-sm provider-test-btn' + (needsHttp ? '' : ' is-disabled'),
    type: 'button', disabled: !needsHttp,
    title: needsHttp ? '测试连通性（主进程直连，10s 超时）' : '该条目无 HTTP 端点（Agent 运行时）',
    onclick: async () => {
      const now = Date.now();
      if (now - (testDebounce.get(p.name) || 0) < 1500) return;
      testDebounce.set(p.name, now);
      testBtn.disabled = true;
      const orig = testBtn.textContent;
      testBtn.textContent = '测试中…';
      resultBox.innerHTML = '';
      try {
        const r = await window.studio.providerTest({ base_url: p.base_url });
        // 延迟颜色分级（借鉴 cc-switch：<300 绿 / <800 黄 / ≥800 红）
        const cls = r.ok
          ? (r.latency_ms < 300 ? 'prov-test-ok' : r.latency_ms < 800 ? 'prov-test-mid' : 'prov-test-slow')
          : 'prov-test-err';
        resultBox.appendChild(h('span', { class: cls, title: r.ok ? 'GET /models 延迟' : '' },
          r.ok ? `${r.latency_ms}ms${r.latency_ms < 300 ? ' 快' : r.latency_ms < 800 ? ' 一般' : ' 慢'}` : (r.error || `HTTP ${r.status}`)));
      } catch (e) {
        resultBox.appendChild(h('span', { class: 'prov-test-err' }, String(e && e.message || e)));
      } finally {
        testBtn.disabled = !needsHttp;
        testBtn.textContent = orig;
      }
    },
  }, '测试');

  // 多端点测速按钮（借鉴 cc-switch EndpointSpeedTest）
  const speedBtn = h('button', {
    class: 'btn btn-sm provider-speed-btn' + (needsHttp ? '' : ' is-disabled'),
    type: 'button', disabled: !needsHttp,
    title: needsHttp ? '多端点测速对比，可添加自定义镜像端点并选用最快节点' : '该条目无 HTTP 端点（Agent 运行时）',
    onclick: () => openEndpointSpeedTest(p),
  }, '测速');

  // 编辑按钮
  const editBtn = h('button', {
    class: 'btn btn-sm prov-edit-btn', type: 'button',
    title: '生成覆盖/新增 YAML 片段（Studio 不直接写 agentcrew.yaml）',
    onclick: () => openEditProviderModal(p),
  }, '编辑');

  const modelsWrap = h('div', { class: 'prov-models hidden' });
  let toggleBtn = null;

  if (isAgent) {
    // Agent 是执行引擎，不是可选模型：只说明入口与配置方式，不给「设为默认模型」
    modelsWrap.classList.remove('hidden');
    modelsWrap.appendChild(h('div', { class: 'prov-agent-hint' },
      `执行入口：${models[0] || 'default'} · 在「新建运行」的 Agent 智能体分组中选择；`
      + (p.command ? '命令见上方。' : '通用 CLI 需在 agentcrew.yaml 的 provider_overrides 里指定 command。')));
  } else {
    // 在线模型拉取行（借鉴 cc-switch FetchedModelPicker：实时 GET /models）
    const fetchStatus = h('span', { class: 'prov-fetch-status' });
    const fetchBtn = h('button', {
      class: 'prov-fetch-btn', type: 'button',
      title: needsHttp ? '从供应商 API 实时拉取可用模型（GET /models，主进程携带环境变量中的 Key）' : '该条目无 HTTP 端点，不能拉取在线模型',
      disabled: !needsHttp,
      onclick: () => openOnlineModelPicker(p, fetchBtn, fetchStatus),
    }, '⟳ 拉取在线模型');
    modelsWrap.appendChild(h('div', { class: 'prov-fetch-row' }, fetchBtn, fetchStatus));

    if (models.length) {
      for (const m of models) {
        const alias = `${p.name}:${m}`;
        const tag = h('span', {
          class: 'prov-model-tag' + ((S.settings && S.settings.defaultModel === alias) ? ' active' : ''),
          title: '点击设为默认模型',
        }, m);
        tag.addEventListener('click', async () => {
          if (!S.settings) S.settings = {};
          S.settings.defaultModel = alias;
          // 视觉反馈：全局只保留当前点击的标签高亮（先清所有卡片，再点亮本标签）
          $$('.prov-model-tag').forEach(x => x.classList.remove('active'));
          tag.classList.add('active');
          try { await window.studio.setSettings({ defaultModel: alias }); toast('success', '已设为默认模型', alias); }
          catch (e) { toast('error', '保存失败', String(e.message || e)); }
        });
        modelsWrap.appendChild(tag);
      }
    } else {
      modelsWrap.appendChild(h('span', { class: 'prov-models-empty' }, '暂无模型，可点击上方"拉取在线模型"获取'));
    }

    toggleBtn = h('button', {
      class: 'btn btn-sm prov-toggle-models', type: 'button',
      onclick: () => {
        modelsWrap.classList.toggle('hidden');
        toggleBtn.textContent = modelsWrap.classList.contains('hidden') ? '模型' : '收起';
      },
    }, '模型');
  }

  // meta 区：密钥环境变量名（+ 获取 Key 链接）、建议模型数、端点候选数、流式状态
  const metaChildren = [];
  if (p.api_key_env) {
    const envSpan = h('span', { class: 'prov-env', title: '环境变量名（点"编辑"可直接保存 Key 到 .env）' }, '密钥：' + p.api_key_env);
    metaChildren.push(envSpan);
    if (p.api_key_url) {
      metaChildren.push(h('button', {
        class: 'prov-getkey', type: 'button', title: '在浏览器打开官方 Key 页面',
        onclick: () => { if (window.studio.openExternal) window.studio.openExternal(p.api_key_url); },
      }, '🔗 获取Key'));
    }
  } else {
    metaChildren.push(h('span', { class: 'prov-env none' }, '无需密钥'));
  }
  metaChildren.push(h('span', { class: 'prov-model-count' },
    isAgent ? `${models.length} 个执行入口` : `${models.length} 个建议模型`));
  if (isAgent) metaChildren.push(h('span', { class: 'prov-agent-note' }, '自带工具循环与认证 · 在新建运行时选择'));
  if (Array.isArray(p.endpoint_candidates) && p.endpoint_candidates.length > 1) {
    metaChildren.push(h('span', { class: 'prov-ep', title: '多端点候选，按顺序回退' }, `${p.endpoint_candidates.length} 端点候选`));
  }
  const streamOk = p.health ? p.health.stream_ok : p.stream_ok;
  if (streamOk === true) {
    metaChildren.push(h('span', { class: 'prov-stream ok', title: '流式输出可用' }, '流式 ✓'));
  } else if (streamOk === false) {
    metaChildren.push(h('span', { class: 'prov-stream err', title: '流式输出不可用' }, '流式 ✗'));
  }

  return h('div', { class: 'prov-card', 'data-provider': p.name, 'data-kind': p.type || 'llm' },
    h('div', { class: 'prov-avatar' }, initial),
    h('div', { class: 'prov-main' },
      h('div', { class: 'prov-title-row' },
        h('span', { class: 'prov-name' }, p.label || p.name),
        typeBadge,
        usageBadge,
        catBadge,
        h('span', { class: 'prov-badge' + (p._builtin ? ' builtin' : ' custom') }, p._builtin ? '内置' : '自定义'),
        statusDot),
      h('div', { class: isAgent ? 'prov-url prov-cmd' : 'prov-url', title: isAgent ? (p.command || '') : '' },
        isAgent ? (p.command || '未配置命令（provider_overrides.command）') : (p.base_url || '（无 HTTP 端点）')),
      h('div', { class: 'prov-meta' }, ...metaChildren),
      resultBox,
      modelsWrap),
    h('div', { class: 'prov-actions' },
      ...(isAgent ? [editBtn] : [testBtn, speedBtn, editBtn, toggleBtn])));
}

/* ============================================================
   在线模型拉取（借鉴 cc-switch FetchedModelPicker + model-fetch）
   ============================================================ */

/* 点击"拉取在线模型"：主进程 GET /models，成功后弹出多选选择器 */
async function openOnlineModelPicker(p, triggerBtn, statusEl) {
  const key = p.key || p.name;
  if (!p.base_url) { toast('warning', '无法拉取', '该供应商无 HTTP 端点'); return; }
  triggerBtn.disabled = true;
  const orig = triggerBtn.textContent;
  triggerBtn.textContent = '拉取中…';
  statusEl.className = 'prov-fetch-status';
  statusEl.textContent = '';
  let r;
  try {
    r = await window.studio.providerFetchModels({
      base_url: p.base_url,
      api_key_env: p.api_key_env || '',
      family: p.family || 'openai_compat',
    });
  } catch (e) {
    statusEl.classList.add('err');
    statusEl.textContent = '拉取失败：' + String(e && e.message || e);
    triggerBtn.disabled = false; triggerBtn.textContent = orig;
    return;
  }
  triggerBtn.disabled = false; triggerBtn.textContent = orig;
  if (!r || !r.ok) {
    const raw = (r && r.error) || '拉取失败';
    const msg = /401|403/.test(raw) ? '鉴权失败（HTTP 401/403），请检查环境变量 ' + (p.api_key_env || 'API Key')
      : /404|405/.test(raw) ? '该供应商未开放 /models 接口，或 Base URL 有误'
      : /超时|timeout/i.test(raw) ? '连接超时（10s）'
      : /解析/.test(raw) ? raw : raw;
    statusEl.classList.add('err');
    statusEl.textContent = msg;
    toast('error', '模型拉取失败', msg);
    return;
  }
  statusEl.classList.add('ok');
  statusEl.textContent = `在线获取 ${r.models.length} 个模型`;
  showFetchedModelsModal(p, r.models);
}

export { buildProviderCard, openOnlineModelPicker };
