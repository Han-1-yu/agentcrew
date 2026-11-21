/**
 * 内置供应商目录与模型别名（对齐后端 config.PROVIDER_PRESETS）。
 * UI 侧唯一一份供应商清单；业务代码不要另抄一份。
 */

const BUILTIN_PROVIDERS = [
  // ── 大模型供应商（模型 ID 于 2026-11-20 按官方文档/模型目录核对）──
  { key:'openai',    label:'OpenAI',    base_url:'https://api.openai.com/v1',                       api_key_env:'OPENAI_API_KEY',    suggested_models:['gpt-6.1-sol','gpt-6-astra','gpt-6-luna','gpt-6.1-terra'],
    category:'international', family:'openai_compat', search_aliases:['gpt','open ai'], endpoint_candidates:null, api_key_url:'https://platform.openai.com/api-keys' },
  { key:'anthropic', label:'Anthropic', base_url:'https://api.anthropic.com',                       api_key_env:'ANTHROPIC_API_KEY', suggested_models:['claude-opus-5-5','claude-sonnet-5-5','claude-haiku-5-5','claude-fable-5-2'],
    category:'international', family:'anthropic', search_aliases:['claude'], endpoint_candidates:null, api_key_url:'https://console.anthropic.com/settings/keys' },
  { key:'deepseek',  label:'DeepSeek',  base_url:'https://api.deepseek.com/v1',                     api_key_env:'DEEPSEEK_API_KEY',  suggested_models:['deepseek-flash','deepseek-v4-pro'],
    category:'national', family:'openai_compat', search_aliases:['ds','深度求索'], endpoint_candidates:null, api_key_url:'https://platform.deepseek.com/api_keys' },
  { key:'doubao',    label:'豆包',       base_url:'https://ark.cn-beijing.volces.com/api/v3',        api_key_env:'ARK_API_KEY',       suggested_models:['doubao-seed-evolving','doubao-seed-2-1-pro-260915','doubao-seed-2-1-lite-260915','doubao-seed-2-1-turbo-260628'],
    category:'national', family:'openai_compat', search_aliases:['豆包','volcengine','火山'], endpoint_candidates:null, api_key_url:'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey' },
  { key:'qwen',      label:'通义千问',   base_url:'https://dashscope.aliyuncs.com/compatible-mode/v1',api_key_env:'DASHSCOPE_API_KEY', suggested_models:['qwen4-max','qwen3.8-max','qwen3.7-plus','qwen3.8-flash'],
    category:'national', family:'openai_compat', search_aliases:['通义','千问','aliyun','阿里云'], endpoint_candidates:null, api_key_url:'https://dashscope.console.aliyun.com/apiKey' },
  { key:'ernie',     label:'文心一言',   base_url:'https://qianfan.baidubce.com/v2',                  api_key_env:'QIANFAN_API_KEY',   suggested_models:['ernie-5.5-flash-preview','ernie-5.1'],
    category:'national', family:'openai_compat', search_aliases:['文心','百度','baidu'], endpoint_candidates:null, api_key_url:'https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application' },
  { key:'ollama',    label:'Ollama',    base_url:'http://localhost:11434/v1',                       api_key_env:'',                   suggested_models:['qwen3','deepseek-r1','llama3.3','mistral'],
    category:'local', family:'openai_compat', search_aliases:['本地','local'], endpoint_candidates:null, api_key_url:null },
  { key:'mock',      label:'Mock',      base_url:null,                                                 api_key_env:'',                   suggested_models:['mock-model'],
    category:'specialized', family:'mock', search_aliases:[], endpoint_candidates:null, api_key_url:null },
  { key:'cli',       label:'自定义 CLI', base_url:null,                                                 api_key_env:'',                   suggested_models:['cli-model'],
    category:'specialized', family:'cli', search_aliases:['本地命令行'], endpoint_candidates:null, api_key_url:null },
  // ── Agent 智能体（真实 CLI 产品；不是模型供应商）──
  { key:'claude-code', label:'Claude Code', base_url:null, api_key_env:'', suggested_models:['default'],
    category:'specialized', family:'cli', search_aliases:['claude code','cc'], endpoint_candidates:null, api_key_url:null,
    command:'claude -p --output-format json --permission-mode acceptEdits', agentic:true },
  { key:'codex',     label:'Codex CLI',  base_url:null, api_key_env:'', suggested_models:['default'],
    category:'specialized', family:'cli', search_aliases:['openai codex','codex cli'], endpoint_candidates:null, api_key_url:null,
    command:'codex exec --json --sandbox workspace-write --skip-git-repo-check', agentic:true },
  // ── 聚合 / 推理平台 ──
  { key:'openrouter',  label:'OpenRouter',  base_url:'https://openrouter.ai/api/v1',                api_key_env:'OPENROUTER_API_KEY',  suggested_models:['openai/gpt-6.1-sol','anthropic/claude-opus-5-5','deepseek/deepseek-v4.1-flash-1017','z-ai/glm-5.3-flash'],
    category:'aggregator', family:'openai_compat', search_aliases:['or','路由'], endpoint_candidates:null, api_key_url:'https://openrouter.ai/keys' },
  // 以下平台的模型目录变化频繁，不在前端硬编码；卡片里用「拉取在线模型」读实时目录
  { key:'siliconflow', label:'SiliconFlow',  base_url:'https://api.siliconflow.cn/v1',              api_key_env:'SILICONFLOW_API_KEY', suggested_models:[],
    category:'national', family:'openai_compat', search_aliases:['硅基流动','sf'], endpoint_candidates:null, api_key_url:'https://cloud.siliconflow.cn/account/ak' },
  { key:'moonshot',    label:'Moonshot',     base_url:'https://api.moonshot.cn/v1',                 api_key_env:'MOONSHOT_API_KEY',    suggested_models:['kimi-k3.1-pro','kimi-k3','kimi-k2.7-code'],
    category:'national', family:'openai_compat', search_aliases:['月之暗面','kimi'], endpoint_candidates:null, api_key_url:'https://platform.moonshot.cn/console/api-keys' },
  { key:'zhipu',       label:'智谱 AI',      base_url:'https://open.bigmodel.cn/api/paas/v4',       api_key_env:'ZHIPU_API_KEY',       suggested_models:['glm-5.3','glm-5.3-flash','glm-5.2'],
    category:'national', family:'openai_compat', search_aliases:['智谱','glm','chatglm'], endpoint_candidates:null, api_key_url:'https://open.bigmodel.cn/usercenter/apikeys' },
  { key:'groq',        label:'Groq',         base_url:'https://api.groq.com/openai/v1',            api_key_env:'GROQ_API_KEY',        suggested_models:[],
    category:'aggregator', family:'openai_compat', search_aliases:['groqcloud'], endpoint_candidates:null, api_key_url:'https://console.groq.com/keys' },
  { key:'together',    label:'Together AI',   base_url:'https://api.together.xyz/v1',               api_key_env:'TOGETHER_API_KEY',    suggested_models:[],
    category:'aggregator', family:'openai_compat', search_aliases:['together ai'], endpoint_candidates:null, api_key_url:'https://api.together.xyz/settings/api-keys' },
  { key:'fireworks',   label:'Fireworks',     base_url:'https://api.fireworks.ai/inference/v1',     api_key_env:'FIREWORKS_API_KEY',   suggested_models:[],
    category:'aggregator', family:'openai_compat', search_aliases:['fireworks ai','烟花'], endpoint_candidates:null, api_key_url:'https://fireworks.ai/account/api-keys' },
  { key:'deepinfra',   label:'DeepInfra',     base_url:'https://api.deepinfra.com/v1/openai',       api_key_env:'DEEPINFRA_API_KEY',   suggested_models:[],
    category:'aggregator', family:'openai_compat', search_aliases:['deep infra'], endpoint_candidates:null, api_key_url:'https://deepinfra.com/dash/api_keys' },
  { key:'replicate',   label:'Replicate',     base_url:'https://openai.replicate.com/v1',           api_key_env:'REPLICATE_API_TOKEN', suggested_models:[],
    category:'aggregator', family:'openai_compat', search_aliases:['replicate','复制'], endpoint_candidates:null, api_key_url:'https://replicate.com/account/api-tokens' },
  { key:'huggingface', label:'HuggingFace',    base_url:'https://router.huggingface.co/v1',           api_key_env:'HF_API_TOKEN',        suggested_models:[],
    category:'aggregator', family:'openai_compat', search_aliases:['hf','hugging face','拥抱脸'], endpoint_candidates:null, api_key_url:'https://huggingface.co/settings/tokens' },
  // ── 国内其他 ──
  { key:'baichuan',    label:'百川',           base_url:'https://api.baichuan-ai.com/v1',             api_key_env:'BAICHUAN_API_KEY',    suggested_models:[],
    category:'national', family:'openai_compat', search_aliases:['百川','baichuan'], endpoint_candidates:null, api_key_url:'https://platform.baichuan-ai.com/console/apikey' },
  { key:'minimax',     label:'MiniMax',        base_url:'https://api.minimax.chat/v1',                 api_key_env:'MINIMAX_API_KEY',     suggested_models:['MiniMax-M3.1-Flash','MiniMax-M3'],
    category:'national', family:'openai_compat', search_aliases:['minimax','海螺'], endpoint_candidates:null, api_key_url:'https://platform.minimaxi.com/user-center/basic-information/interface-key' },
  { key:'stepfun',     label:'阶跃星辰',       base_url:'https://api.stepfun.com/v1',                  api_key_env:'STEPFUN_API_KEY',    suggested_models:['step-5-preview'],
    category:'national', family:'openai_compat', search_aliases:['阶跃星辰','step'], endpoint_candidates:null, api_key_url:'https://platform.stepfun.com/interface/key' },
  { key:'lingyi',      label:'零一万物',       base_url:'https://api.lingyiwanwu.com/v1',             api_key_env:'YI_API_KEY',         suggested_models:[],
    category:'national', family:'openai_compat', search_aliases:['零一万物','01ai','yi'], endpoint_candidates:null, api_key_url:'https://platform.lingyiwanwu.com/apikeys' },
  { key:'xinghuo',     label:'讯飞星火',       base_url:'https://spark-api-open.xf-yun.com/v1',       api_key_env:'XINGHUO_API_KEY',     suggested_models:[],
    category:'national', family:'openai_compat', search_aliases:['讯飞','星火','xfyun'], endpoint_candidates:null, api_key_url:'https://console.xfyun.cn/services/cbm' },
  // ── 国际 ──
  { key:'gemini',      label:'Gemini',         base_url:'https://generativelanguage.googleapis.com/v1beta/openai', api_key_env:'GEMINI_API_KEY', suggested_models:['gemini-4-carbon','gemini-4-argon','gemini-4-hydrogen','gemini-3.8-flash'],
    category:'international', family:'openai_compat', search_aliases:['gemini','google','谷歌','bard'], endpoint_candidates:null, api_key_url:'https://aistudio.google.com/app/apikey' },
  { key:'mistral',     label:'Mistral',        base_url:'https://api.mistral.ai/v1',                  api_key_env:'MISTRAL_API_KEY',    suggested_models:['mistral-large-4','mistral-large-latest'],
    category:'international', family:'openai_compat', search_aliases:['mistral','米斯特拉尔'], endpoint_candidates:null, api_key_url:'https://console.mistral.ai/api-keys' },
  { key:'cohere',      label:'Cohere',         base_url:'https://api.cohere.com/v2',                  api_key_env:'CO_API_KEY',          suggested_models:['command-a-plus-05-2026','command-a'],
    category:'international', family:'openai_compat', search_aliases:['cohere','coral'], endpoint_candidates:null, api_key_url:'https://dashboard.cohere.com/api-keys' },
];
// ── 供应商类型：模型供应商 / Agent 智能体 / 内部测试替身 ──────────────
// 只有 type=llm 才是「大模型供应商」，参与默认模型、模型切换器与模型目录；
// Agent 智能体是带工具循环的 CLI 执行引擎，不能当成模型列；mock 只是离线替身，
// 不进入供应商板块。
const AGENT_PROVIDER_KEYS = new Set(['cli', 'claude-code', 'codex']);
const INTERNAL_PROVIDER_KEYS = new Set(['mock']);
const PROVIDER_TYPE_LABELS = { llm: '大模型', agent: 'Agent 智能体', internal: '内部' };

function providerType(p) {
  const key = p && (p.key || p.name);
  if (INTERNAL_PROVIDER_KEYS.has(key)) return 'internal';
  if (p && p.family === 'mock') return 'internal';
  if (AGENT_PROVIDER_KEYS.has(key)) return 'agent';
  if (p && p.family === 'cli') return 'agent';
  return 'llm';
}
for (const p of BUILTIN_PROVIDERS) p.type = providerType(p);

// ── 调用量（公开代理指标，2026-11-20 核对）────────────────────────────
// 大模型：OpenRouter「作者请求份额」周榜（2026-11-09 当周）。它不是全市场调用量，
// 但口径统一、可复核；企业 API 支出份额单独放在 spendShare，只作提示，不参与排序。
// Agent：Who Burned More 观测样本的「编程 Agent Token 份额」（2026-11-18，215 名开发者），
// 同样不是全市场；没有公开数据的条目不参与排名。
const PROVIDER_USAGE = {
  deepseek:  { share: 31.8, rank: 1, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周' },
  openai:    { share: 19.0, rank: 2, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周', spendShare: 31 },
  gemini:    { share: 15.1, rank: 3, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周', spendShare: 9 },
  zhipu:     { share: 7.0,  rank: 4, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周' },
  anthropic: { share: 6.3,  rank: 5, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周', spendShare: 55 },
  qwen:      { share: 6.2,  rank: 6, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周' },
  mistral:   { share: 2.6,  rank: 7, metric: 'OpenRouter 请求份额', asOf: '2026-11-09 周' },
};
const AGENT_USAGE = {
  'claude-code': { share: 60, rank: 1, metric: '编程 Agent Token 份额', asOf: '2026-11-18' },
  codex:         { share: 33, rank: 2, metric: '编程 Agent Token 份额', asOf: '2026-11-18' },
};
for (const p of BUILTIN_PROVIDERS) {
  p.usage = p.type === 'agent' ? (AGENT_USAGE[p.key] || null) : (PROVIDER_USAGE[p.key] || null);
}

// 扁平模型别名列表（供设置页默认模型选择器使用）：只收大模型，避免
// 把 Claude Code / Codex 这类执行引擎混进「默认模型」。
const MODEL_ALIASES = BUILTIN_PROVIDERS
  .filter(p => p.type === 'llm')
  .flatMap(p => p.suggested_models.map(m => ({
    provider: p.key, model: m, label: `${m} (${p.label})`,
  })));
// 兼容旧调用：接受 {provider, model} 对象或 "provider:model" 字符串
const modelAlias = (x) => typeof x === 'string' ? x : `${x.provider}:${x.model}`;
const findProvider = (key) => BUILTIN_PROVIDERS.find(p => p.key === key) || null;

// e2e / 开发者控制台按名字取内置目录（重构前是脚本级全局，保持可见性不变）
window.BUILTIN_PROVIDERS = BUILTIN_PROVIDERS;

export {
  AGENT_PROVIDER_KEYS, AGENT_USAGE, BUILTIN_PROVIDERS, INTERNAL_PROVIDER_KEYS,
  MODEL_ALIASES, PROVIDER_TYPE_LABELS, PROVIDER_USAGE, findProvider, modelAlias, providerType,
};
