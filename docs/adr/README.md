# 架构决策记录（ADR）

本目录记录 AgentCrew 里**有取舍、不显然、会影响后续维护**的架构决策。与
`../THREAT_MODEL.md`（回答「防得住什么」）互补：ADR 回答「为什么当初这么做、
现在还能不能改」。

## 格式

每条 ADR 四段：**状态**（提议 / 已接受 / 已替代，附编号）→ **背景**（问题与约束）→
**决策**（做了什么、明确不做）→ **后果**（得到什么、付出什么、何时重审）。

## 索引

| # | 决策 | 状态 |
|---|---|---|
| [0001](0001-auth-fail-closed.md) | 认证 fail-closed：无密钥一律拒绝启动 | 已接受 |
| [0002](0002-run-python-off-by-default.md) | `run_python` 默认不注册 | 已接受 |
| [0003](0003-native-tools-shared-consumer.md) | 原生 function calling 与文本围栏共用消费端 | 已接受 |
| [0004](0004-otel-zero-dep-otlp.md) | OTel 零依赖 OTLP/HTTP JSON | 已接受 |
| [0005](0005-guardrails-deterministic-only.md) | guardrails 只用确定性规则，不用 LLM 判 | 已接受 |
| [0006](0006-evals-deterministic-no-judge.md) | evals 用确定性断言 + pass^k，不用 LLM judge | 已接受 |
| [0007](0007-mcp-streamable-http-http-only.md) | MCP streamable HTTP 自实现，仅 http（TLS 反代） | 已接受 |

## 维护

- 新决策先写「提议」，评审通过改「已接受」；被推翻的标「已替代」并指向新编号。
- 改了决策对应的默认值或行为，**必须**同步更新对应 ADR 的「后果/重审」段——
  这与 `THREAT_MODEL.md` §9 的维护规则同一条纪律。
