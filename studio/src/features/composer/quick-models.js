/**
 * 命令框「快速切换模型」的短名单与展示元数据。
 *
 * 为什么不直接摊平 BUILTIN_PROVIDERS：输入框旁的下拉只服务「一句话目标」这个
 * 高频场景，十几行扫一眼就能选；全量目录仍在「切换模型」弹窗里。
 *
 * costWeight 是**相对成本估算**（1.00x = 基准模型，按输入单价折算），只用来横向
 * 比较、不参与计费，真实账单以 run 的 ledger 为准。数值集中在这里，换真价目表时
 * 只改这一处；alias 必须能在 contracts/providers.js 里找到，否则下拉里没有图标。
 */

const QUICK_MODEL_ALIASES = [
  // 顺序按 2026-11 的公开调用量/使用份额近似排：高价旗舰不超过低价高频模型
  'deepseek:deepseek-flash',
  'openai:gpt-6.1-sol',
  'gemini:gemini-4-hydrogen',
  'zhipu:glm-5.3-flash',
  'anthropic:claude-opus-5-5',
  'qwen:qwen3.8-flash',
  'moonshot:kimi-k3.1-pro',
  'doubao:doubao-seed-2-1-lite-260915',
  'anthropic:claude-sonnet-5-5',
  'zhipu:glm-5.3',
  'deepseek:deepseek-v4-pro',
  'ollama:qwen3',
];

//: tone ∈ violet | green | amber | gray（对应 composer.css 里的 .cmm-badge-*）
const MODEL_UI_META = {
  'deepseek:deepseek-flash': { costWeight: 0.08, badge: { text: '低价', tone: 'green' } },
  'openai:gpt-6.1-sol': { costWeight: 1.00, badge: { text: '旗舰', tone: 'violet' } },
  'gemini:gemini-4-hydrogen': { costWeight: 0.75 },
  'zhipu:glm-5.3-flash': { costWeight: 0.05 },
  'anthropic:claude-opus-5-5': { costWeight: 2.00 },
  'qwen:qwen3.8-flash': { costWeight: 0.08 },
  'moonshot:kimi-k3.1-pro': { costWeight: 1.00 },
  'doubao:doubao-seed-2-1-lite-260915': { costWeight: 0.06 },
  'anthropic:claude-sonnet-5-5': { costWeight: 1.00 },
  'zhipu:glm-5.3': { costWeight: 0.70, badge: { text: '订阅', tone: 'amber' } },
  'deepseek:deepseek-v4-pro': { costWeight: 0.33 },
  'ollama:qwen3': { costWeight: 0.00, badge: { text: '本地', tone: 'gray' } },
};

/** 未知 alias 返回空对象：下拉照样能选，只是没有价格/标签。 */
function metaOf(alias) {
  return MODEL_UI_META[alias] || {};
}

export { MODEL_UI_META, QUICK_MODEL_ALIASES, metaOf };
