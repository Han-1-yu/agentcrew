/**
 * 配额弹窗：日 / 周 / 月 token 上限与当前用量。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';
function openQuotaModal() {
  const quotas = (S.settings && S.settings.quotas) || {};
  const rows = [];

  const buildRow = (alias, q) => {
    q = q || {};
    const daily = q.daily || {};
    const weekly = q.weekly || {};
    const monthly = q.monthly || {};
    const aliasInput = h('input', { class: 'quota-alias-input', type: 'text', value: alias || '', placeholder: 'provider:model，如 openai:gpt-4o' });
    const dailyCalls = h('input', { class: 'quota-num', type: 'number', min: '0', value: daily.max_calls || '', placeholder: '次数' });
    const dailyTokens = h('input', { class: 'quota-num', type: 'number', min: '0', value: daily.max_tokens || '', placeholder: 'Token' });
    const dailyCost = h('input', { class: 'quota-num', type: 'number', min: '0', step: '0.01', value: daily.max_cost_usd || '', placeholder: '$' });
    const weeklyCalls = h('input', { class: 'quota-num', type: 'number', min: '0', value: weekly.max_calls || '', placeholder: '次数' });
    const weeklyTokens = h('input', { class: 'quota-num', type: 'number', min: '0', value: weekly.max_tokens || '', placeholder: 'Token' });
    const weeklyCost = h('input', { class: 'quota-num', type: 'number', min: '0', step: '0.01', value: weekly.max_cost_usd || '', placeholder: '$' });
    const monthlyCalls = h('input', { class: 'quota-num', type: 'number', min: '0', value: monthly.max_calls || '', placeholder: '次数' });
    const monthlyTokens = h('input', { class: 'quota-num', type: 'number', min: '0', value: monthly.max_tokens || '', placeholder: 'Token' });
    const monthlyCost = h('input', { class: 'quota-num', type: 'number', min: '0', step: '0.01', value: monthly.max_cost_usd || '', placeholder: '$' });
    const delBtn = h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => { rowEl.remove(); } }, '删除');
    const rowEl = h('div', { class: 'quota-row' },
      h('div', { class: 'quota-row-head' },
        aliasInput, delBtn),
      h('div', { class: 'quota-grid' },
        h('div', { class: 'quota-col' },
          h('div', { class: 'quota-col-title' }, '日'),
          dailyCalls, dailyTokens, dailyCost),
        h('div', { class: 'quota-col' },
          h('div', { class: 'quota-col-title' }, '周'),
          weeklyCalls, weeklyTokens, weeklyCost),
        h('div', { class: 'quota-col' },
          h('div', { class: 'quota-col-title' }, '月'),
          monthlyCalls, monthlyTokens, monthlyCost)));
    rows.push({ aliasInput, daily: { dailyCalls, dailyTokens, dailyCost }, weekly: { weeklyCalls, weeklyTokens, weeklyCost }, monthly: { monthlyCalls, monthlyTokens, monthlyCost }, rowEl });
    return rowEl;
  };

  const listEl = h('div', { class: 'quota-list' });
  Object.entries(quotas).forEach(([alias, q]) => listEl.appendChild(buildRow(alias, q)));

  const addBtn = h('button', { class: 'btn btn-sm', type: 'button', onclick: () => {
    listEl.appendChild(buildRow('', {}));
  } }, '+ 添加别名配额');

  const saveBtn = h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
    const next = {};
    for (const r of rows) {
      const alias = r.aliasInput.value.trim();
      if (!alias) continue;
      const collect = (obj) => {
        const out = {};
        if (obj.dailyCalls?.value) out.max_calls = parseInt(obj.dailyCalls.value) || 0;
        if (obj.dailyTokens?.value) out.max_tokens = parseInt(obj.dailyTokens.value) || 0;
        if (obj.dailyCost?.value) out.max_cost_usd = parseFloat(obj.dailyCost.value) || 0;
        return Object.keys(out).length ? out : undefined;
      };
      // 修正字段名映射
      const d = {};
      if (r.daily.dailyCalls.value) d.max_calls = parseInt(r.daily.dailyCalls.value) || 0;
      if (r.daily.dailyTokens.value) d.max_tokens = parseInt(r.daily.dailyTokens.value) || 0;
      if (r.daily.dailyCost.value) d.max_cost_usd = parseFloat(r.daily.dailyCost.value) || 0;
      const w = {};
      if (r.weekly.weeklyCalls.value) w.max_calls = parseInt(r.weekly.weeklyCalls.value) || 0;
      if (r.weekly.weeklyTokens.value) w.max_tokens = parseInt(r.weekly.weeklyTokens.value) || 0;
      if (r.weekly.weeklyCost.value) w.max_cost_usd = parseFloat(r.weekly.weeklyCost.value) || 0;
      const m = {};
      if (r.monthly.monthlyCalls.value) m.max_calls = parseInt(r.monthly.monthlyCalls.value) || 0;
      if (r.monthly.monthlyTokens.value) m.max_tokens = parseInt(r.monthly.monthlyTokens.value) || 0;
      if (r.monthly.monthlyCost.value) m.max_cost_usd = parseFloat(r.monthly.monthlyCost.value) || 0;
      const quota = {};
      if (Object.keys(d).length) quota.daily = d;
      if (Object.keys(w).length) quota.weekly = w;
      if (Object.keys(m).length) quota.monthly = m;
      if (Object.keys(quota).length) next[alias] = quota;
    }
    if (!S.settings) S.settings = {};
    S.settings.quotas = next;
    S.settings.quota_enabled = Object.keys(next).length > 0;
    try {
      await window.studio.setSettings({ quotas: next, quota_enabled: S.settings.quota_enabled });
      toast('success', '配额已保存', `${Object.keys(next).length} 个别名`);
      closeModal();
    } catch (e) { toast('error', '保存失败', String(e.message || e)); }
  } }, '保存配额');

  showModal(h('div', { class: 'ap-modal quota-modal' },
    h('h3', { class: 'modal-title' }, '多窗口配额管理 (V7)'),
    h('div', { class: 'ap-hint' }, '按别名设置日/周/月配额。配额耗尽时自动触发故障转移到下一个可用别名。留空表示不限制。'),
    h('div', { class: 'quota-header' },
      h('span', { class: 'quota-h-alias' }, '别名'),
      h('span', { class: 'quota-h-col' }, '日 (calls / tokens / $)'),
      h('span', { class: 'quota-h-col' }, '周 (calls / tokens / $)'),
      h('span', { class: 'quota-h-col' }, '月 (calls / tokens / $)'),
      h('span', { class: 'quota-h-action' }, '')),
    listEl,
    addBtn,
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', onclick: closeModal }, '取消'),
      saveBtn)));
}

/* ============================================================
   事件日志页（全量）
   ============================================================ */

export { openQuotaModal };
