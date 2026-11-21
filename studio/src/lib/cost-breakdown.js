/**
 * 成本按模型 / 供应商聚合（纯函数，node:test 直测）。
 *
 * 唯一数据源是 ledger 的 `llm_call` 事件：它带 role / model / token /
 * estimated_cost_usd。事件里的 `model` 是真实模型名（如 `deepseek-chat`），
 * 不一定带供应商前缀；因此先用别名前缀（`deepseek:deepseek-chat`）判断，
 * 再拿 run.json 的 default_model 做同模型匹配，都推不出时留空（不编造）。
 */

/** 从模型名推断供应商 key；无法确定时返回空串。 */
function providerOf(model, defaultModel) {
  const name = String(model || '').trim();
  if (!name) return '';
  const cut = name.indexOf(':');
  if (cut > 0) return name.slice(0, cut);
  const def = String(defaultModel || '').trim();
  const defCut = def.indexOf(':');
  if (defCut > 0 && def.slice(defCut + 1) === name) return def.slice(0, defCut);
  return '';
}

/**
 * @param {Array<{event?: string, data?: object}>} events ledger 事件（正序）
 * @param {{defaultModel?: string, limit?: number}} [opts] limit<=0 表示不截断
 * @returns {{rows: Array, total: number, max: number, restCount: number}}
 */
function aggregateCostByModel(events, opts = {}) {
  const defaultModel = opts.defaultModel || '';
  const limit = opts.limit == null ? 6 : Number(opts.limit);
  const map = new Map();
  for (const e of events || []) {
    if (!e || e.event !== 'llm_call') continue;
    const d = e.data || {};
    const model = String(d.model || '').trim() || '未知模型';
    const provider = providerOf(model, defaultModel);
    const key = provider ? `${provider}/${model}` : model;
    const row = map.get(key) || {
      key, provider, model, calls: 0, cost: 0, prompt: 0, completion: 0,
    };
    row.calls += 1;
    row.cost += Number(d.estimated_cost_usd) || 0;
    row.prompt += Number(d.prompt_tokens) || 0;
    row.completion += Number(d.completion_tokens) || 0;
    map.set(key, row);
  }
  const all = [...map.values()].sort((a, b) => b.cost - a.cost || b.calls - a.calls);
  const total = all.reduce((sum, r) => sum + r.cost, 0);
  const rows = limit > 0 ? all.slice(0, limit) : all;
  return {
    rows,
    total,
    max: Math.max(...all.map((r) => r.cost), 0.000001),
    restCount: Math.max(0, all.length - rows.length),
  };
}

export { aggregateCostByModel, providerOf };
