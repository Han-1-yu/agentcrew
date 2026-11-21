# 供应商板块梳理：大模型供应商与 Agent 智能体（2026-11-20）

## 1. 问题

原供应商页把 27 个预设平铺：Mock（离线测试替身）、CLI（执行引擎）与真实模型厂商混在一起，
既没有排序规则，也看不出「谁提供模型、谁提供执行能力」。后端 `PROVIDER_PRESETS` 里已有的
Claude Code / Codex 两个 Agent 预设，前端也没有展示。

## 2. 分层规则

| 类型 | 判定 | 是否进入「供应商」 | 说明 |
|---|---|---|---|
| 大模型供应商 `llm` | 提供 chat/completions 或本地推理服务 | 是 | 参与默认模型、模型切换器、模型目录 |
| Agent 智能体 `agent` | `family=cli`（Claude Code / Codex / 通用 CLI） | 是，但独立分区 | 自带工具循环与认证的执行引擎，不是模型 API |
| 内部替身 `internal` | `family=mock` | **否** | 仅用于离线演练；在「新建运行」勾选 Mock 即可 |

当前清单（25 + 3，Mock 隐藏；模型 ID 与调用量见 `docs/MODEL-CATALOG-2026-11-20.md`）：

- **已上榜调用量（7）**：DeepSeek、OpenAI、Gemini、智谱 AI、Anthropic、通义千问、Mistral
- **未上榜（18）**：豆包、Moonshot、SiliconFlow、MiniMax、阶跃星辰、百川、文心一言、零一万物、讯飞星火、
  Cohere、OpenRouter、Groq、Together AI、Fireworks、DeepInfra、Replicate、HuggingFace、Ollama
- **Agent 智能体 3**：Claude Code、Codex CLI、自定义 CLI（无公开调用量）

## 3. 排序规则

1. 大模型在前，Agent 智能体在后；
2. **大模型按公开调用量（OpenRouter 作者请求份额，2026-11-09 周）**：
   DeepSeek 31.8% → OpenAI 19.0% → Gemini 15.1% → 智谱 7.0% → Anthropic 6.3% →
   通义 6.2% → Mistral 2.6%；
3. 未上榜的大模型按 国内 → 国际 → 聚合 → 本地 → 常用名次 → 名称；
4. Agent 按编程 Agent Token 份额（2026-11-18 观测）：Claude Code 60% → Codex CLI 33% →
   自定义 CLI（无公开数据）。

**口径说明**：OpenRouter 不是全市场调用量；企业 API 支出份额（Menlo 2026：Anthropic 55%、
OpenAI 31%、Google 9%）只放在徽标 tooltip 里作参考，不参与排序。数据来源与真实模型清单见
`docs/MODEL-CATALOG-2026-11-20.md`。规则写在 `studio/src/lib/providers.js`。

## 4. UI 行为

- 页面标题改为「供应商与智能体」；类型 tabs：全部 28 / 大模型 25 / Agent 智能体 3。
- 「全部」视图按两个分区渲染，分区头部有标题、数量与一句定位说明。
- 卡片加类型徽标（大模型 / Agent 智能体）与调用量徽标（`#1 31.8%`）；Agent 卡片紫色左边线、
  紫色头像、显示 CLI 命令与「执行入口」说明。
- Agent 卡片不提供「设为默认模型」与 HTTP 测试/测速（它们没有模型 API），只保留「编辑」。
- 分类 chips 只筛大模型；选 Agent 视图时 chips 置灰，排序说明变为「按名称」。
- 「从预设添加」与「新建运行」的供应商下拉都按两个 optgroup 分组；Mock 不再出现。
- 模型切换器、设置页默认模型只收大模型，不会把 Claude Code / Codex 当成模型。
- 模型 ID 只保留 2026-11-20 能按官方目录核对的真实值；目录变化快的平台不在前端硬编码，
  统一走「拉取在线模型」。

## 5. 代码位置

| 文件 | 作用 |
|---|---|
| `src/contracts/providers.js` | 类型判定 `providerType`、Agent 预设、类型标签、模型别名过滤 |
| `src/lib/providers.js` | 排序策略（分类 + 常用名次 + 名称） |
| `src/features/providers/index.js` | 类型 tabs、分区渲染、筛选与排序说明 |
| `src/features/providers/card.js` | 类型徽标、Agent 卡片差异、命令与入口说明 |
| `src/features/new-run/provider-options.js` | 新建运行的供应商 optgroup 分组 |
| `src/features/providers/{models,forms}.js` | 模型切换器 / 预设添加排除 Agent 与 Mock |

## 6. 验证

- 架构检查：90 模块 / 46 样式分片；
- 单测：**59/59**（新增 `providers.test.mjs`：类型判定、别名过滤、调用量排序与稳定性）；
- e2e：**36/36**（供应商页新增「类型 tabs / 两分区 / 隐藏 Mock」「Agent 与大模型分开」两项）；
- 视觉基线：23 张截图一致（新增 Agent 视图截图）。
- Python 后端未改。
