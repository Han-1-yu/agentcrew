# ADR-0007：MCP streamable HTTP 自实现，仅 http（TLS 反代）

- **状态**：已接受（2026-11-08）
- **关联**：`agentcrew/mcp/transports.py`；`THREAT_MODEL.md` G-15

## 背景

P3-9 的 `HTTPTransport` 是「POST 一次拿一个 JSON」，与官方 streamable HTTP 差会话、
协议版本、SSE 响应、会话失效四件事，每一件在真实服务端上都会失败。本仓 HTTP 层手写，
引入 `mcp` 官方 SDK 会带进一堆依赖。

## 决策

- 自实现 streamable HTTP：`Mcp-Session-Id` 回带、`MCP-Protocol-Version` 按协商值、
  `text/event-stream` 解析（解 chunk → 跨块缓冲成行 → 匹配 id 即停）、404 抛
  `MCPSessionExpired` 由 client 重建一次。
- 加响应双上限（8 MiB / 1000 事件）——响应靠 id 匹配结束，没有上限会永久阻塞。
- **仅支持 `http://`**：TLS 与主服务同一约定，由反向代理终止。

## 后果

- 得到：与官方 server 互通所需的最小语义，零新依赖。
- 付出：未实现可选的 GET 主动消息通道与会话 `DELETE`；远程 MCP 必须走内网/反代。
- 重审：接入公网 MCP 或需要服务端主动推送时，再补 TLS 与 GET 通道。
