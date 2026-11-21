/**
 * 供应商列表的排序策略（纯函数，供页面和单测共用）。
 *
 * 规则：先按类型（大模型 → Agent 智能体）→ 再按公开调用量排名 →
 * 未上榜的按 国内→国际→聚合→本地 → 常用名次 → 名称。
 * 调用量口径见 contracts/providers.js 的 PROVIDER_USAGE/AGENT_USAGE。
 */
const CAT_RANK = { national: 0, international: 1, aggregator: 2, local: 3, specialized: 4 };

//: 组内「常用优先」的固定名次；未收录的供应商排在已收录之后，再按名称。
//: 只影响展示顺序，不参与调度、不改变任何 provider 能力。
const POPULARITY = {
  deepseek: 0, doubao: 1, qwen: 2, zhipu: 3, moonshot: 4, siliconflow: 5,
  minimax: 6, stepfun: 7, baichuan: 8, ernie: 9, lingyi: 10, xinghuo: 11,
  openai: 0, anthropic: 1, gemini: 2, mistral: 3, cohere: 4,
  openrouter: 0, groq: 1, together: 2, fireworks: 3, deepinfra: 4, replicate: 5, huggingface: 6,
  ollama: 0,
  'claude-code': 0, codex: 1, cli: 2,
};

function rankOf(p) {
  const key = p && (p.key || p.name);
  const v = POPULARITY[key];
  return v == null ? 100 : v;
}

function usageRank(p) {
  const u = p && p.usage;
  return u && u.rank ? u.rank : 999;
}

function sortProviders(list) {
  return [...(list || [])].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'llm' ? -1 : 1;
    const ua = usageRank(a);
    const ub = usageRank(b);
    if (ua !== ub) return ua - ub;
    const ca = CAT_RANK[a.category] == null ? 9 : CAT_RANK[a.category];
    const cb = CAT_RANK[b.category] == null ? 9 : CAT_RANK[b.category];
    if (ca !== cb) return ca - cb;
    const ra = rankOf(a);
    const rb = rankOf(b);
    if (ra !== rb) return ra - rb;
    return String(a.label || a.name || '').localeCompare(String(b.label || b.name || ''), 'zh-Hans-CN');
  });
}

export { CAT_RANK, POPULARITY, sortProviders, usageRank };
