# ADR-0004：OTel 零依赖 OTLP/HTTP JSON

- **状态**：已接受（2026-11-08）
- **关联**：`runtime/otel.py`；`otel_*` 配置；`THREAT_MODEL.md` G-10

## 背景

官方 `opentelemetry-sdk` 会拖进 api/sdk/exporter + protobuf 一串依赖，与本仓「HTTP
服务、SDK、工具链全手写、依赖白名单极严」冲突。OTLP 协议本身支持 HTTP + JSON 编码，
`urllib` + `json` 就够。

## 决策

- 只实现一条够用的通道：`OTLPTracer` 缓冲 span，run 收尾一次性 POST
  `{endpoint}/v1/traces`，JSON 编码，一次 run 一个 `traceId`。
- 借用 `gen_ai.*` 语义，**不自造新 `gen_ai.*` 键**，接 Langfuse/Phoenix/Tempo 免映射。
- 默认 `otel_enabled=false`、`otel_capture_content=false`（内容不采集）。

## 后果

- 得到：零新依赖的分布式追踪，可观测性不影响 run 结局（导出失败只 warning）。
- 付出：功能是 OTel 的子集——没有 batch/重试/采样策略；跨 run 关联、父子采样要靠
  collector。
- 重审：需要完整 OTel 生态（SDK 级采样、传播、metrics）时，再评估引入 SDK。
