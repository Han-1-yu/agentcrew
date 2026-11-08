# ADR-0003：原生 function calling 与文本围栏共用消费端

- **状态**：已接受（2026-11-08）
- **关联**：`P2-1`；`CompletionResult.tool_calls`；`_run_agent_loop`

## 背景

引入原生 function calling 时有两种做法：A) 为原生写一条独立的执行链路；B) 只让
「调用从哪来」不同，解析之后的执行、校验、权限、留痕、错误回灌走同一条路。A 的
风险是两条链路漂移——同样的工具在原生模式与围栏模式下行为不一致，而「同任务同结果」
恰恰是验收标准。

## 决策

选 B。`completion.tool_calls`（原生）与 `<tool_call>` 文本围栏，都转成同一
`ParsedToolCall` 列表，交给同一个 `_execute_tool_calls`。区别只到回灌协议为止：
原生用 `assistant(tool_calls)` + 逐条 `role="tool"`，围栏用 `<tool_result>` 块。

## 后果

- 得到：同一工具、同一任务，两种模式产出一致；新增工具无需关心走哪条协议。
- 付出：数据层多一层「归一化」，且原生路径要正确回传 `tool_call_id`（多一套消息形状）。
- 重审：若某 provider 需要协议特异的工具语义（非 OpenAI 形状），在消费端加适配，
  不回退成独立链路。
