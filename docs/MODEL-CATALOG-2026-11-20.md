# 大模型 / Agent 真实性与调用量核查（2026-11-20）

## 1. 口径

- **真实存在性**：以厂商官方模型目录 / 发布说明为准，逐个核对 API 模型 ID；核对不到的不写进前端。
- **调用量**：用 **OpenRouter 作者请求份额**（2026-11-09 当周）作为公开代理指标。它不是全市场
  调用量，但口径统一、可复核；**企业 API 支出份额**（Menlo Ventures 2026）只作为并列参考，
  不参与排序——两者结论并不一致（按支出 Anthropic 第一，按请求 DeepSeek 第一）。
- **Agent**：用 Who Burned More 的观测样本（2026-11-18，215 名开发者）的 **Token 份额**；
  BenchGecko 的 HN mindshare 作为旁证。样本非全市场，因此只用于同榜单内排序。

## 2. 大模型调用量排序（本页顺序）

| # | 供应商 | OpenRouter 请求份额 | 企业 API 支出份额 | 说明 |
|---|---|---|---|---|
| 1 | DeepSeek | 31.8% | — | 低价 Flash 占据平台超四分之一 token |
| 2 | OpenAI | 19.0% | 31% | GPT-6 Luna/Sol 走量 |
| 3 | Google（Gemini） | 15.1% | 9% | 请求份额高、企业支出份额下滑 |
| 4 | 智谱 AI（Z.ai） | 7.0% | — | GLM-5.3 Flash 走量 |
| 5 | Anthropic | 6.3% | 55% | 支出第一、请求量第五：高价 + 编程场景 |
| 6 | 阿里（通义千问） | 6.2% | — | Qwen3.8/Qwen4 系列 |
| 7 | Mistral | 2.6% | — | Mistral Large 4 |
| — | 腾讯 / 英伟达 | 2.3% / 1.6% | — | 未在 AgentCrew 内置目录，需自定义 provider |

其余内置供应商（豆包、MiniMax、阶跃、百度、Cohere 等）未进入 OpenRouter 作者前九，按分类与
名称排在已上榜者之后，不伪造份额。

## 3. 已核对的真实模型 ID

| 供应商 | 真实模型（2026-11-20） | 替换掉的旧/虚构值 |
|---|---|---|
| OpenAI | `gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-luna`、`gpt-6.1-terra` | `gpt-5.6-terra` |
| Anthropic | `claude-opus-5-5`、`claude-sonnet-5-5`、`claude-haiku-5-5`、`claude-fable-5-2` | `claude-fable-5-1`、`claude-haiku-4-5` |
| DeepSeek | `deepseek-flash`（V4.1 Flash 1017）、`deepseek-v4-pro`（V4 Pro 0813） | `deepseek-v4-flash`（已下线别名） |
| 豆包 | `doubao-seed-evolving`、`doubao-seed-2-1-pro-260915`、`-lite-260915`、`-turbo-260628` | — |
| 通义千问 | `qwen4-max`、`qwen3.8-max`、`qwen3.7-plus`、`qwen3.8-flash` | 缺 `qwen4-max` |
| 文心一言 | `ernie-5.5-flash-preview`、`ernie-5.1` | `ernie-4.5-*` |
| Moonshot | `kimi-k3.1-pro`、`kimi-k3`、`kimi-k2.7-code` | `kimi-k2.5` |
| 智谱 | `glm-5.3`、`glm-5.3-flash`、`glm-5.2` | `glm-5.3-flashx`（保留但非首选） |
| MiniMax | `MiniMax-M3.1-Flash`、`MiniMax-M3` | `MiniMax-M2.7` |
| 阶跃星辰 | `step-5-preview` | `step-3.7/3.5-flash` |
| Gemini | `gemini-4-carbon`、`gemini-4-argon`、`gemini-4-hydrogen`、`gemini-3.8-flash` | `gemini-2.5-*`（已弃用/限流） |
| Mistral | `mistral-large-4`、`mistral-large-latest` | `mistral-medium-latest` |
| Cohere | `command-a-plus-05-2026`、`command-a` | `command-a-03-2025` |
| OpenRouter | `openai/gpt-6.1-sol`、`anthropic/claude-opus-5-5`、`deepseek/deepseek-v4.1-flash-1017`、`z-ai/glm-5.3-flash` | `deepseek/deepseek-v4-pro` |
| Ollama | `qwen3`、`deepseek-r1`、`llama3.3`、`mistral` | — |

**不硬编码**：SiliconFlow、Groq、Together、Fireworks、DeepInfra、Replicate、HuggingFace、百川、
零一万物、讯飞星火的模型目录变化快或缺官方 ID 证据，前端 `suggested_models` 置空，统一走
「拉取在线模型」读实时目录，避免再次出现「假模型」。

## 4. Agent 智能体

| # | 产品 | Token 份额（观测） | 开发者覆盖率 | 说明 |
|---|---|---|---|---|
| 1 | Claude Code | 60% | 77% | 终端 Agent 第一，主力模型 `claude-opus-5-5` |
| 2 | Codex CLI | 33% | 60% | 主力模型 `gpt-6.1-sol` |
| — | 自定义 CLI | 无公开数据 | — | 适配器，不参与排名 |

旁证：BenchGecko mindshare（2026-11-13）Claude Code 28.8%、GitHub Copilot 12.1%、OpenCode
11.7%、Cursor 10.0%、Codex CLI 7.7%、Gemini CLI 0.33%。不同口径排序不同，但 Claude Code
与 Codex 的前二位置一致。

AgentCrew 只内置了真正接通的 Claude Code / Codex，以及通用 CLI 适配器；OpenCode、Cursor、
Gemini CLI 等未内置，不在此页冒充可选 Agent。

## 5. 落地规则

1. 列表顺序：类型（大模型 → Agent）→ 公开调用量排名 → 未上榜按分类/常用/名称。
2. 卡片显示 `#1 31.8%` 徽标，tooltip 写明「指标 + 日期 + 企业支出份额」；未上榜不显示徽标。
3. 「调用量」只影响展示顺序，不参与调度、预算或路由。
4. 模型目录更新只改 `contracts/providers.js` 一处；命令框短名单同步（`quick-models.js`）。

## 6. 参考

- OpenRouter Rankings（2026-11，作者请求份额/模型 token）：<https://openrouter.ai/rankings>
- BenchLeader Usage（2026-11-20）：<https://www.benchleader.com/usage>
- Menlo Ventures《2026 State of Enterprise AI》：<https://mnlo.vc/enterprise-ai-26>
- Who Burned More（Agent token 份额，2026-11-18）：<https://whoburnedmore.com/agents>
- BenchGecko Coding Agent Mindshare（2026-11-13）：<https://www.benchgecko.ai/mindshare/agents>
- OpenAI GPT-6 模型指南：<https://openai.com/index/practical-guide-building-gpt-6/>
- Anthropic 模型总览（2026-11-05）：<https://bigolab.com/guides/3451-claude-api-model-id-models-api>
- DeepSeek API 文档（V4.1 Flash）：<https://api-docs.deepseek.com/zh-cn/news/news260910/>
- Gemini 4 模型目录：<https://aistudio.google.com/docs/models>
- 阿里云百炼模型列表：<https://help.aliyun.com/en/model-studio/text-generation-model>
- 火山方舟模型列表：<https://console.volcengine.com/ark/region%3Acn-beijing/docs/model-list>
