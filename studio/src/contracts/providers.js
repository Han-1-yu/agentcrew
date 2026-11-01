/**
 * 内置供应商目录与模型别名（对齐后端 config.PROVIDER_PRESETS）。
 * UI 侧唯一一份供应商清单；业务代码不要另抄一份。
 */

const BUILTIN_PROVIDERS = [
  // ── 核心 9 个 ──
  { key:'openai',    label:'OpenAI',    base_url:'https://api.openai.com/v1',                       api_key_env:'OPENAI_API_KEY',    suggested_models:['gpt-6.1-sol','gpt-6-astra','gpt-6-sol','gpt-6-luna','gpt-5.6-terra'],
    category:'international', family:'openai_compat', search_aliases:['gpt','open ai'], endpoint_candidates:null, api_key_url:'https://platform.openai.com/api-keys' },
  { key:'anthropic', label:'Anthropic', base_url:'https://api.anthropic.com',                       api_key_env:'ANTHROPIC_API_KEY', suggested_models:['claude-fable-5-1','claude-opus-5-5','claude-sonnet-5-5','claude-haiku-4-5'],
    category:'international', family:'anthropic', search_aliases:['claude'], endpoint_candidates:null, api_key_url:'https://console.anthropic.com/settings/keys' },
  { key:'deepseek',  label:'DeepSeek',  base_url:'https://api.deepseek.com/v1',                     api_key_env:'DEEPSEEK_API_KEY',  suggested_models:['deepseek-v4-pro','deepseek-v4-flash'],
    category:'national', family:'openai_compat', search_aliases:['ds','深度求索'], endpoint_candidates:null, api_key_url:'https://platform.deepseek.com/api_keys' },
  { key:'doubao',    label:'豆包',       base_url:'https://ark.cn-beijing.volces.com/api/v3',        api_key_env:'ARK_API_KEY',       suggested_models:['doubao-seed-evolving','doubao-seed-2-1-pro-260915','doubao-seed-2-1-lite-260915','doubao-seed-2-1-turbo-260628'],
    category:'national', family:'openai_compat', search_aliases:['豆包','volcengine','火山'], endpoint_candidates:null, api_key_url:'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey' },
  { key:'qwen',      label:'通义千问',   base_url:'https://dashscope.aliyuncs.com/compatible-mode/v1',api_key_env:'DASHSCOPE_API_KEY', suggested_models:['qwen3.8-max','qwen3.7-plus','qwen3.8-flash'],
    category:'national', family:'openai_compat', search_aliases:['通义','千问','aliyun','阿里云'], endpoint_candidates:null, api_key_url:'https://dashscope.console.aliyun.com/apiKey' },
  { key:'ernie',     label:'文心一言',   base_url:'https://qianfan.baidubce.com/v2',                  api_key_env:'QIANFAN_API_KEY',   suggested_models:['ernie-4.5-turbo-128k','ernie-4.0','ernie-speed'],
    category:'national', family:'openai_compat', search_aliases:['文心','百度','baidu'], endpoint_candidates:null, api_key_url:'https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application' },
  { key:'ollama',    label:'Ollama',    base_url:'http://localhost:11434/v1',                       api_key_env:'',                   suggested_models:['qwen3','llama3.3','deepseek-r1','mistral'],
    category:'local', family:'openai_compat', search_aliases:['本地','local'], endpoint_candidates:null, api_key_url:null },
  { key:'mock',      label:'Mock',      base_url:null,                                                 api_key_env:'',                   suggested_models:['mock'],
    category:'specialized', family:'mock', search_aliases:[], endpoint_candidates:null, api_key_url:null },
  { key:'cli',       label:'CLI',       base_url:null,                                                 api_key_env:'',                   suggested_models:[],
    category:'specialized', family:'cli', search_aliases:[], endpoint_candidates:null, api_key_url:null },
  // ── 额外预设 6 个（原 EXTRA_PRESETS + together）──
  { key:'openrouter',  label:'OpenRouter',  base_url:'https://openrouter.ai/api/v1',                api_key_env:'OPENROUTER_API_KEY',  suggested_models:['openai/gpt-6.1-sol','anthropic/claude-opus-5-5','deepseek/deepseek-v4-pro'],
    category:'aggregator', family:'openai_compat', search_aliases:['or','路由'], endpoint_candidates:null, api_key_url:'https://openrouter.ai/keys' },
  { key:'siliconflow', label:'SiliconFlow',  base_url:'https://api.siliconflow.cn/v1',              api_key_env:'SILICONFLOW_API_KEY', suggested_models:['deepseek-ai/DeepSeek-V4','Qwen/Qwen3-72B'],
    category:'national', family:'openai_compat', search_aliases:['硅基流动','sf'], endpoint_candidates:null, api_key_url:'https://cloud.siliconflow.cn/account/ak' },
  { key:'moonshot',    label:'Moonshot',     base_url:'https://api.moonshot.cn/v1',                 api_key_env:'MOONSHOT_API_KEY',    suggested_models:['kimi-k2.5','kimi-k2-turbo-preview','kimi-k2-thinking'],
    category:'national', family:'openai_compat', search_aliases:['月之暗面','kimi'], endpoint_candidates:null, api_key_url:'https://platform.moonshot.cn/console/api-keys' },
  { key:'zhipu',       label:'智谱 AI',      base_url:'https://open.bigmodel.cn/api/paas/v4',       api_key_env:'ZHIPU_API_KEY',       suggested_models:['glm-5.3','glm-5.3-flash','glm-5.3-flashx','glm-5.2'],
    category:'national', family:'openai_compat', search_aliases:['智谱','glm','chatglm'], endpoint_candidates:null, api_key_url:'https://open.bigmodel.cn/usercenter/apikeys' },
  { key:'groq',        label:'Groq',         base_url:'https://api.groq.com/openai/v1',            api_key_env:'GROQ_API_KEY',        suggested_models:['llama-3.3-70b-versatile','openai/gpt-oss-120b','llama-3.1-8b-instant'],
    category:'aggregator', family:'openai_compat', search_aliases:['groqcloud'], endpoint_candidates:null, api_key_url:'https://console.groq.com/keys' },
  { key:'together',    label:'Together AI',   base_url:'https://api.together.xyz/v1',               api_key_env:'TOGETHER_API_KEY',    suggested_models:['meta-llama/Llama-3.3-70B-Instruct-Turbo'],
    category:'aggregator', family:'openai_compat', search_aliases:['together ai'], endpoint_candidates:null, api_key_url:'https://api.together.xyz/settings/api-keys' },
  // ── V5 P1 新增 12 个：聚合 4 ──
  { key:'fireworks',   label:'Fireworks',     base_url:'https://api.fireworks.ai/inference/v1',     api_key_env:'FIREWORKS_API_KEY',   suggested_models:['accounts/fireworks/models/llama-v3p3-70b-instruct'],
    category:'aggregator', family:'openai_compat', search_aliases:['fireworks ai','烟花'], endpoint_candidates:null, api_key_url:'https://fireworks.ai/account/api-keys' },
  { key:'deepinfra',   label:'DeepInfra',     base_url:'https://api.deepinfra.com/v1/openai',       api_key_env:'DEEPINFRA_API_KEY',   suggested_models:['meta-llama/Llama-3.3-70B-Instruct'],
    category:'aggregator', family:'openai_compat', search_aliases:['deep infra'], endpoint_candidates:null, api_key_url:'https://deepinfra.com/dash/api_keys' },
  { key:'replicate',   label:'Replicate',     base_url:'https://openai.replicate.com/v1',           api_key_env:'REPLICATE_API_TOKEN', suggested_models:['meta/meta-llama-3.3-70b-instruct'],
    category:'aggregator', family:'openai_compat', search_aliases:['replicate','复制'], endpoint_candidates:null, api_key_url:'https://replicate.com/account/api-tokens' },
  { key:'huggingface', label:'HuggingFace',    base_url:'https://router.huggingface.co/v1',           api_key_env:'HF_API_TOKEN',        suggested_models:['meta-llama/Llama-3.3-70B-Instruct'],
    category:'aggregator', family:'openai_compat', search_aliases:['hf','hugging face','拥抱脸'], endpoint_candidates:null, api_key_url:'https://huggingface.co/settings/tokens' },
  // ── V5 P1 新增：国内 5 ──
  { key:'baichuan',    label:'百川',           base_url:'https://api.baichuan-ai.com/v1',             api_key_env:'BAICHUAN_API_KEY',    suggested_models:['Baichuan4-Turbo','Baichuan4-Air'],
    category:'national', family:'openai_compat', search_aliases:['百川','baichuan'], endpoint_candidates:null, api_key_url:'https://platform.baichuan-ai.com/console/apikey' },
  { key:'minimax',     label:'MiniMax',        base_url:'https://api.minimax.chat/v1',                 api_key_env:'MINIMAX_API_KEY',     suggested_models:['MiniMax-M3','MiniMax-M2.7'],
    category:'national', family:'openai_compat', search_aliases:['minimax','海螺'], endpoint_candidates:null, api_key_url:'https://platform.minimaxi.com/user-center/basic-information/interface-key' },
  { key:'stepfun',     label:'阶跃星辰',       base_url:'https://api.stepfun.com/v1',                  api_key_env:'STEPFUN_API_KEY',    suggested_models:['step-5-preview','step-3.7-flash','step-3.5-flash'],
    category:'national', family:'openai_compat', search_aliases:['阶跃星辰','step'], endpoint_candidates:null, api_key_url:'https://platform.stepfun.com/interface/key' },
  { key:'lingyi',      label:'零一万物',       base_url:'https://api.lingyiwanwu.com/v1',             api_key_env:'YI_API_KEY',         suggested_models:['yi-lightning','yi-large','yi-large-turbo'],
    category:'national', family:'openai_compat', search_aliases:['零一万物','01ai','yi'], endpoint_candidates:null, api_key_url:'https://platform.lingyiwanwu.com/apikeys' },
  { key:'xinghuo',     label:'讯飞星火',       base_url:'https://spark-api-open.xf-yun.com/v1',       api_key_env:'XINGHUO_API_KEY',     suggested_models:['spark-x','4.0Ultra','generalv3.5'],
    category:'national', family:'openai_compat', search_aliases:['讯飞','星火','xfyun'], endpoint_candidates:null, api_key_url:'https://console.xfyun.cn/services/cbm' },
  // ── V5 P1 新增：国际 3 ──
  { key:'gemini',      label:'Gemini',         base_url:'https://generativelanguage.googleapis.com/v1beta/openai', api_key_env:'GEMINI_API_KEY', suggested_models:['gemini-2.5-pro','gemini-2.5-flash','gemini-2.5-flash-lite','gemini-2.0-flash'],
    category:'international', family:'openai_compat', search_aliases:['gemini','google','谷歌','bard'], endpoint_candidates:null, api_key_url:'https://aistudio.google.com/app/apikey' },
  { key:'mistral',     label:'Mistral',        base_url:'https://api.mistral.ai/v1',                  api_key_env:'MISTRAL_API_KEY',    suggested_models:['mistral-large-latest','mistral-medium-latest'],
    category:'international', family:'openai_compat', search_aliases:['mistral','米斯特拉尔'], endpoint_candidates:null, api_key_url:'https://console.mistral.ai/api-keys' },
  { key:'cohere',      label:'Cohere',         base_url:'https://api.cohere.com/v2',                  api_key_env:'CO_API_KEY',          suggested_models:['command-a-plus-05-2026','command-a-03-2025','command-a-reasoning-08-2025'],
    category:'international', family:'openai_compat', search_aliases:['cohere','coral'], endpoint_candidates:null, api_key_url:'https://dashboard.cohere.com/api-keys' },
];
// 扁平模型别名列表（供设置页默认模型选择器使用）
const MODEL_ALIASES = BUILTIN_PROVIDERS.flatMap(p => p.suggested_models.map(m => ({
  provider: p.key, model: m, label: `${m} (${p.label})`,
})));
// 兼容旧调用：接受 {provider, model} 对象或 "provider:model" 字符串
const modelAlias = (x) => typeof x === 'string' ? x : `${x.provider}:${x.model}`;
const findProvider = (key) => BUILTIN_PROVIDERS.find(p => p.key === key) || null;

// e2e / 开发者控制台按名字取内置目录（重构前是脚本级全局，保持可见性不变）
window.BUILTIN_PROVIDERS = BUILTIN_PROVIDERS;

export { BUILTIN_PROVIDERS, MODEL_ALIASES, findProvider, modelAlias };
