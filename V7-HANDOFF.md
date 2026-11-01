# V7 完善交接文档（云电脑 → 本地接力）

> 生成时间：2026-10-10 23:50
> 基于：magpie-main / cc-haha-main / cc-switch-main 三项目深度阅读后的 V7 增强

---

## 一、当前进度总览

| 模块 | 状态 | 测试 | 说明 |
|------|------|------|------|
| **P0 数据脱敏** (redact.py) | ✅ 完成 | 21/21 通过 | 借鉴 magpie redact，API Key/PII/凭据正则匹配+确定性占位符，已集成到 call/stream_call/complete_structured |
| **P1 多模式路由** (factory.py) | ✅ 完成 | 16/16 通过 | 借鉴 magpie routing，新增 rotate/usage/order 三种策略 + 原有 weighted，usage 带指数衰减 |
| **P2 多窗口配额** (quota.py) | 🔶 部分完成 | 未写测试 | quota.py 模块 + config.py 已完成；**factory.py 集成未做**，测试未写 |
| **P3 会话亲和性** (affinity.py) | ⬜ 未开始 | — | 借鉴 magpie affinity，对话保持在同一模型利用 prompt 缓存 |

---

## 二、已修改/新增的文件

### 新增文件
- `agentcrew/models/redact.py` — 数据脱敏核心模块（9.7KB）
- `agentcrew/models/quota.py` — 多窗口配额核心模块（11.1KB）
- `tests/test_redact_v7.py` — P0 测试（21 个用例）
- `tests/test_routing_v7.py` — P1 测试（16 个用例）

### 修改文件
- `agentcrew/config.py` — 新增 9 个配置字段：
  - `redact_enabled` / `redact_api_keys` / `redact_pii` / `redact_credentials` / `redact_custom_words`
  - `routing_strategy`
  - `quotas` / `quota_enabled`
  - 对应环境变量映射已添加
- `agentcrew/models/factory.py` — P0 脱敏集成 + P1 路由策略 + P2 预留
  - `_make_redactor()` / `_redact_result()` 辅助方法
  - `_CollectingProvider` 支持 redactor 参数
  - `_select_weighted()` 重写为多策略分发
  - `_select_rotate()` / `_select_usage()` / `_note_usage()` / `_current_usage_score()`
  - `_account()` 新增 `alias` 参数，5 个调用点已传入
  - call / _call_weighted / stream_call / complete_structured / _complete_structured_weighted 均已集成脱敏

---

## 三、云电脑上需要完成的工作

### 步骤 1：完成 P2 factory 集成

在 `agentcrew/models/factory.py` 中：

1. **导入 QuotaTracker 和 QuotaExhaustedError**：
   ```python
   from agentcrew.models.quota import QuotaExhaustedError, QuotaTracker, parse_quota_config
   ```

2. **在 `__init__` 中初始化 QuotaTracker**：
   ```python
   from agentcrew.models.quota import parse_quota_config
   self._quota_tracker = QuotaTracker(
       state_path=settings.output_dir / "quota_state.json",
       quotas=parse_quota_config(settings.quotas) if settings.quota_enabled else {},
   )
   ```

3. **在 call / _call_weighted 的 provider 调用前添加配额检查**：
   ```python
   # 在 async with sem: 之前
   try:
       self._quota_tracker.check(current_alias)  # 或 chosen
   except QuotaExhaustedError:
       # 配额耗尽视为 Provider 不可用，触发故障转移
       if idx < len(aliases) - 1:
           self._emit_failover(current_alias, aliases[idx + 1], exc)
           continue
       raise
   ```

4. **在 `_account` 中记录配额消耗**（已有 alias 参数，直接加）：
   ```python
   if alias is not None:
       self._quota_tracker.consume(alias, result.prompt_tokens, result.completion_tokens, cost)
   ```

5. **在 `reset_stats()` 中重置配额使用量**（可选，不重置也可以，配额是跨 run 的）。

### 步骤 2：写 P2 测试

创建 `tests/test_quota_v7.py`，覆盖：
- QuotaLimit / AliasQuota 配置解析
- daily/weekly/monthly 窗口滚动重置
- check() 耗尽时抛 QuotaExhaustedError
- consume() 累计使用量
- 持久化（state_path 写入/读取）
- parse_quota_config 从 dict 解析
- 与 ModelFactory 集成（mock provider + 配额配置，验证耗尽后故障转移）

### 步骤 3：实现 P3 会话亲和性（可选，时间充裕时做）

1. 创建 `agentcrew/models/affinity.py`：
   - `AffinityTracker` 类，维护 session_id → alias 映射
   - 支持 `session` / `turn` / `off` 三种模式
   - 持久化到 `output/affinity_state.json`

2. 在 config.py 添加：
   - `affinity_mode: str = "off"`（默认关闭，向后兼容）

3. 在 factory.py 的 call 方法中：
   - 调用前根据 session_id 查找亲和性别名
   - 调用成功后记录亲和性
   - 亲和性别名不可用时回退到正常路由

### 步骤 4：运行全量测试

```bash
cd E:\agentcrew
.\.venv-ci\Scripts\python.exe -m pytest tests/ -x --tb=short -q
```

确保：
- 原有 1810 个测试仍然全部通过
- 新增 P0(21) + P1(16) + P2(?) 测试全部通过
- 总数 = 1810 + 21 + 16 + P2数量

### 步骤 5：更新文档

- 更新 `PROJECT-STATE.md`：添加 V7 章节
- 更新 `README.md`：添加新配置项说明
- 更新 `HANDOFF.md`：记录 V7 完成状态

---

## 四、代码同步方案（云电脑 → 本地）

### 方案 A：Git（推荐，如果项目已初始化 git）

```bash
# 云电脑上
cd E:\agentcrew
git add agentcrew/config.py agentcrew/models/factory.py agentcrew/models/redact.py agentcrew/models/quota.py tests/test_redact_v7.py tests/test_routing_v7.py tests/test_quota_v7.py
git commit -m "V7: 数据脱敏 + 多模式路由 + 多窗口配额"
git push origin v7-enhancements

# 本地电脑上
cd E:\agentcrew
git pull origin v7-enhancements
```

### 方案 B：直接复制文件（无 git 时）

需要复制以下文件到本地 `E:\agentcrew\` 对应位置：

```
agentcrew/config.py              (修改)
agentcrew/models/factory.py      (修改)
agentcrew/models/redact.py       (新增)
agentcrew/models/quota.py        (新增)
tests/test_redact_v7.py          (新增)
tests/test_routing_v7.py         (新增)
tests/test_quota_v7.py           (新增，云电脑完成后)
V7-HANDOFF.md                    (本文档)
```

用压缩包方式：
```bash
# 云电脑上打包
cd E:\agentcrew
Compress-Archive -Path agentcrew\config.py, agentcrew\models\factory.py, agentcrew\models\redact.py, agentcrew\models\quota.py, tests\test_redact_v7.py, tests\test_routing_v7.py, tests\test_quota_v7.py, V7-HANDOFF.md -DestinationPath v7-transfer.zip

# 本地解压覆盖
Expand-Archive -Path v7-transfer.zip -DestinationPath E:\agentcrew -Force
```

### 方案 C：云盘同步

把上述文件放到云盘（飞书/百度网盘/OneDrive），本地下载覆盖。

---

## 五、本地接力步骤

1. **同步代码**：按方案 A/B/C 把云电脑的修改同步到本地 `E:\agentcrew\`

2. **验证环境**：
   ```bash
   cd E:\agentcrew
   .\.venv-ci\Scripts\python.exe --version
   .\.venv-ci\Scripts\python.exe -c "import agentcrew; print(agentcrew.__version__)"
   ```

3. **运行测试**：
   ```bash
   .\.venv-ci\Scripts\python.exe -m pytest tests/test_redact_v7.py tests/test_routing_v7.py tests/test_quota_v7.py -v
   .\.venv-ci\Scripts\python.exe -m pytest tests/ -x --tb=short -q
   ```

4. **如果 P3 未完成**，按本文档第三节步骤 3 继续实现。

5. **最终验证**：全量测试通过后，更新 PROJECT-STATE.md 和 README.md。

---

## 六、关键设计决策记录

### P0 数据脱敏
- **确定性占位符**：`<REDACT:sha1前8位>`，同一原文始终映射到同一占位符，模型回复中引用时可精确还原
- **零侵入**：`redact_enabled=False`（默认）时所有方法为恒等操作，性能开销可忽略
- **覆盖入口**：call / stream_call（逐块还原）/ complete_structured（通过 _CollectingProvider）

### P1 多模式路由
- **向后兼容**：默认 `weighted`，与 V3-10 行为完全一致
- **健康感知**：所有策略都先过滤 `unavailable`，`degraded` 降权
- **usage 衰减**：指数衰减半衰期 1 小时，借鉴 magpie `usageHalfLife`
- **rotate 独立计数**：按 pool 内容哈希作为 key，不同 pool 互不干扰

### P2 多窗口配额（设计已完成，集成待做）
- **窗口边界**：按本地自然日/周/月（周一为周起始）
- **持久化**：`output/quota_state.json`，跨 run 保留
- **故障转移**：配额耗尽视为 Provider 不可用，自动切换到下一个备用别名
- **与全局预算共存**：全局 `max_cost_usd` 是硬上限，配额是按别名的软限制

### P3 会话亲和性（设计待实现）
- **目的**：多 key 池化时，对话保持在同一 key 上利用厂商 prompt 缓存
- **模式**：`session`（整个会话固定）/ `turn`（每轮可换）/ `off`（关闭）
- **回退**：亲和性别名不可用时自动回退到正常路由策略

---

## 七、风险与注意事项

1. **config.py 的 extra="forbid"**：新增字段不会破坏旧配置，但旧配置文件中如果有未知字段会报错——这是原有行为，V7 未改变
2. **factory.py 改动较大**：P0 脱敏集成到了 5 个调用路径，P1 重写了 `_select_weighted`，建议全量测试重点关注 `test_factory.py` 和 `test_crew.py`
3. **配额持久化路径**：`output/quota_state.json` 会在首次调用时创建，属于运行时产物，不需要提交到版本控制
4. **mock provider 测试**：P0/P1 的集成测试用 `Settings(mock=True)`，不消耗真实 API 额度
5. **Python 版本**：项目使用 Python 3.14（.venv-ci），新代码使用了 `from __future__ import annotations` 和 `dict[str, Any]` 类型注解，兼容 3.9+

---

## 八、快速验证命令

```bash
# 只跑 V7 新增测试
.\.venv-ci\Scripts\python.exe -m pytest tests/test_redact_v7.py tests/test_routing_v7.py tests/test_quota_v7.py tests/test_affinity_v7.py -v

# 跑 factory 相关测试（验证未破坏原有功能）
.\.venv-ci\Scripts\python.exe -m pytest tests/test_factory.py -v

# 全量
.\.venv-ci\Scripts\python.exe -m pytest tests/ -x --tb=short -q
```

---

## 九、V7 质量修复（重新深度阅读参考项目后）

> 2026-10-11 本地接力：重新阅读 magpie routing.go/affinity.go/redact.go/known.go/budget.go 后，发现并修复以下设计差距。

### 修复 1：流式脱敏 chunk 边界处理（redact.py + factory.py）

**问题**：`stream_call` 中逐块调用 `unmask_text`，当占位符 `<REDACT:xxxxxxxx>` 被拆分成两个 chunk（如第一块以 `<REDACT:ab` 结尾）时还原失败。

**修复**：
- `redact.py` 新增 `unmask_stream_chunk(chunk)` 和 `unmask_stream_flush()` 方法
- 维护 `_stream_buffer`，检测 chunk 末尾的不完整占位符前缀并缓冲到下一块
- `factory.py` 的 `stream_call` 改用 `unmask_stream_chunk`，流结束后调用 `unmask_stream_flush` 冲刷

**借鉴**：magpie `internal/redact/writer.go` 的流式还原包装器。

### 修复 2：亲和性 cacheCold 超时与记录上限（affinity.py）

**问题**：原实现 TTL 默认 7 天且无记录数上限，与 magpie 设计差距大——厂商 prompt 缓存通常 5 分钟失效，亲和性应随之解除。

**修复**：
- 新增常量 `CACHE_COLD_SECONDS = 300`（借鉴 magpie `cacheCold = 5 * time.Minute`）
- `resolve()` 中检查：超过 5 分钟未使用的亲和性记录视为缓存已冷，返回 primary 重新路由
- 新增常量 `MAX_RECORDS = 512`（借鉴 magpie `sticksKept = 512`）
- `remember()` 中清理过期记录 + 超出上限时删除最旧记录

### 修复 3：配额预扣机制（quota.py + factory.py）

**问题**：原实现仅 `check()` + `consume()`，并发请求可能同时通过检查导致超配额。

**修复**：
- `quota.py` 新增 `reserve()` / `release()` / `settle()` / `_get_reserved()` 方法
- `check()` 中将 in-flight 预扣量计入已使用量：`used + reserved >= limit` 时拒绝
- `factory.py` 的 `call()` 和 `_call_weighted()` 中：
  - `check()` 通过后立即 `reserve(est_tokens)`
  - 成功后 `release()` + `_account()`（内部 `consume()`），release 与 consume 之间无 await 保证原子性
  - 失败时 `release()` 释放预扣
- 新增模块级函数 `_estimate_request_tokens()`：请求体字节数/4 + max_tokens（或默认500）

**借鉴**：magpie `internal/budget/budget.go` 的 reservation 机制（请求体大小 + 平均输出预扣）。

### 仍存在的设计差距（评估后决定不修复）

| 差距 | 来源 | 不修复原因 |
|------|------|-----------|
| redact known values 跨请求记忆 | magpie known.go | agentcrew 用确定性 SHA1 占位符，同一原文始终映射同一占位符，跨请求天然一致 |
| redact 更丰富规则集（ghp_/IBAN/SSN等） | magpie redact.go | 现有规则覆盖主流场景，扩展规则可通过 `redact_custom_words` 配置 |
| routing smart 模式（综合健康+用量+失败） | magpie routing.go | weighted + 健康感知已覆盖 90% 场景，smart 可作为 V8 增强 |
| routing failures 连续失败计数 | magpie routing.go | V6 已有冷却矩阵，失败计数可融入冷却策略 |
| affinity turn 检测（tool_result 判断） | magpie affinity.go | agentcrew 的 turn 概念与 magpie 不同，session/turn 模式已足够 |
| affinity leastHeld（新会话最少持有优先） | magpie affinity.go | 多 key 池化场景下 weighted 路由已能分散负载 |
| affinity why 字段详细归因 | magpie affinity.go | 调试增强，可通过日志补充 |
| budget usage.jsonl 追加式持久化 | magpie budget.go | agentcrew 用独立状态文件，与 ledger 事件系统解耦 |
| budget cacheRead/cacheWrite 细分 | magpie budget.go | provider 返回的 usage 字段不统一，细分成本高收益低 |

### 测试结果

- V7 模块测试：73/73 通过（redact 21 + routing 16 + quota 18 + affinity 18）
- 核心回归测试：648/648 通过（factory 77 + crew/config/provider 112 + agent/ledger/cache 184 + api/cli/gatekeeper/planner/reviewer 178 + scheduler 24 + V7 73）
- 未运行：test_api_server.py（需启动服务器，运行较慢，与 V7 修改无直接关联）

---

## 十、V7 前端完善（AgentCrew Studio）

> 2026-10-11 本地接力：根据 V7 四个后端功能，完善 Electron 桌面端 Studio 的 UI 配置与状态展示。

### 修改的文件

- `studio/index.html` — 侧边栏新增 V7 状态行（路由/亲和性/脱敏）
- `studio/renderer.js` — 设置页新增"网关高级功能"组 + 配额管理模态框 + renderChrome 状态更新
- `studio/styles.css` — 新增复选框行、配额模态框、V7 状态行样式

### 1. 设置页：网关高级功能组

在"模型与预算"组之后新增"网关高级功能 (V7)"组，包含：

**数据脱敏**
- 总开关（启用后子选项才可操作）
- API Key / 个人信息 / 凭据 三个子开关
- 自定义词输入框（逗号分隔）

**路由策略**（segmented control）
- 权重随机 / 轮询 / 最少使用 / 顺序

**会话亲和性**（segmented control）
- 关闭 / 会话保持 / 每轮可换
- 亲和性记录保留天数（1-30，默认 7）

**多窗口配额**
- "管理配额"按钮 → 打开配额管理模态框

### 2. 配额管理模态框

- 按别名（provider:model）配置日/周/月配额
- 每个窗口三个维度：调用次数 / Token 数 / 成本 (USD)
- 支持添加/删除别名配额
- 留空表示不限制
- 保存后自动设置 `quota_enabled`

### 3. 侧边栏 V7 状态展示

在侧边栏底部元信息区新增三行状态：
- **路由**：当前路由策略（权重随机/轮询/最少使用/顺序）
- **亲和性**：当前模式（关闭/会话保持/每轮可换），启用时蓝色高亮
- **脱敏**：启用/关闭，启用时绿色高亮

窄屏（<1024px）下自动隐藏，与现有侧边栏折叠行为一致。

### 4. 保存逻辑

设置页"保存"按钮已扩展，包含全部 V7 字段：
```javascript
{
  redactEnabled, redactApiKeys, redactPii, redactCredentials, redactCustomWords,
  routingStrategy, affinityMode, affinityTtlDays,
  quotas, quota_enabled  // 配额模态框独立保存
}
```

### 设计原则

- 遵循现有设计系统（设计令牌、控件高度、圆角、间距）
- 复用现有 segmented control、settings-field、set-input 组件
- 新功能标注 "(V7)" 便于识别
- 窄屏响应式兼容


## 十一、供应商前端增强（学习 cc-switch，2026-10-11）

> 深度阅读 `E:\deepseek idea\cc-switch-main`（Tauri/Rust+React）的 model-fetch、FetchedModelPicker、EndpointSpeedTest、presetGroups、ProviderCardActions 后，将三项最有价值的供应商管理能力移植到 AgentCrew Studio。
> 全部代码已写入并通过 `node -c` 语法检查（renderer/main/preload 均 exit=0）；**未启动应用，功能测试由本地电脑接力**。

### 11.1 模型切换器重构（分组 + 搜索 + 分类筛选）

`renderer.js :: openModelSwitcher()` 完全重写：
- 模态加宽至 680px（`#modal` 加 `.msw-modal` class，关闭时延时 220ms 移除）
- 搜索框 150ms 防抖，匹配模型名 / 供应商 label / key / search_aliases
- 分类 chips：全部 / 国际 / 国内 / 聚合 / 本地 / 专用（复用 `.prov-cat-btn` 样式）
- 按供应商分组、可折叠：默认只展开当前模型所在组，搜索时强制全部展开
- 组头：avatar + label + 分类徽标 + 模型数 + ▾ chevron
- 当前模型按钮带 ✓ 高亮；模型名用等宽字体
- 底部保留自定义 `provider:model` 输入（回车或点"应用"）
- 新增全局 `getEffectiveProviderModels(p)`：内置 suggested_models + 用户在线添加的 `settings.providerModels[key]` 去重合并（兼容 p.key || p.name）

### 11.2 在线模型实时拉取（GET /models）

**main.js（主进程，Key 不出主进程）**
- 新增顶层函数 `buildModelsUrlCandidates(baseUrl, family)`：
  1. 直接拼 `/models`；
  2. 无版本段（/vN、/openai）时补 `/v1/models`（覆盖 Anthropic 无版本号 base_url）；
  3. 剥离 `/anthropic`、`/claude`、`/compatible-mode` 兼容子路径后兜底。
- 新增 `parseModelsPayload(json)`：兼容三种响应
  - OpenAI/Anthropic/智谱v4：`{data:[{id, owned_by}]}`
  - 智谱Responses/Cohere/Ollama：`{models:[{slug|name|id}]}`
- 新增 IPC `provider:fetchModels`：
  - Key 只从 `process.env[api_key_env]` 读取，不回传渲染进程
  - anthropic family 用 `x-api-key` + `anthropic-version: 2023-06-01`；其余 `Authorization: Bearer`
  - 10s 超时，候选 URL 逐试，返回 `{ok, models:[{id,owned_by}], url, status, authed}`

**preload.js**：暴露 `providerFetchModels(opts)`、`providerTestEndpoints(opts)`

**renderer.js**
- 供应商卡片展开模型区顶部新增 `.prov-fetch-row`：「⟳ 拉取在线模型」按钮 + 状态文字
- `openOnlineModelPicker(p, btn, statusEl)`：调 IPC，401/403（鉴权）、404/405（无此接口）、超时、解析失败分类中文报错
- `showFetchedModelsModal(p, fetched)`：680px 多选弹窗
  - 搜索框（120ms 防抖，匹配 id / owned_by）
  - 复选框列表，已存在的模型禁用并标"已添加"
  - 「添加选中(n)」批量写入 `S.settings.providerModels[key]` → setSettings 持久化 → 刷新供应商页
- API provider 与内置预设合并时补齐 `family` / `suggested_models` / `label`（enriched map）

### 11.3 多端点测速对比

**main.js**：新增 IPC `provider:testEndpoints`，接 `{urls:[...]}`，`Promise.all` 并行 GET（8s 超时），保序返回 `{ok, results:[{url, ok, latency_ms, status, error}]}`

**renderer.js :: openEndpointSpeedTest(p)**（680px 模态）
- 种子端点：当前 base_url + `endpoint_candidates`（兼容字符串数组 / `{url}` 数组），种子项不可移除
- 自定义端点输入：回车/「添加」，new URL 校验、仅 http/https、去尾斜杠、去重，失败 toast
- 「⚡ 开始测速」：base URL 统一映射为 `/models` 后并行测速，结果按索引映射回 base URL（Promise.all 保序）
- 结果按延迟升序、失败排后；最快成功行高亮 `.best` 并标「⚡ 最快」，当前端点标「（当前）」
- 延迟颜色分级：<300ms 绿 / <800ms 黄 / ≥800ms 红 / 失败灰色（`.ep-lat.good|warn|bad|fail`，hover title 显示错误）
- 每行「采用」→ 关闭测速框并打开编辑弹窗（override YAML，base_url 预填为该端点）
- 自定义端点行额外有「移除」
- 卡片单点「测试」结果同步加颜色分级与"快/一般/慢"文案；卡片操作区新增「测速」按钮（顺序：测试 / 测速 / 编辑 / 模型）

**styles.css**：新增四组样式
- `.msw-*`（模型切换器：搜索、chips、分组、chevron、自定义输入）
- `.prov-fetch-*`（拉取行按钮与状态 ok/err）
- `.fm-*`（在线模型多选列表）
- `.ep-*`（测速结果行、dot、延迟分级、采用按钮）
- `.prov-test-mid` / `.prov-test-slow`（单点测试延迟分级）

### 11.4 新增 settings 字段

- `providerModels: { [providerKey]: string[] }` — 用户从在线列表添加的模型，与内置 suggested_models 合并显示，模型切换器中可选

### 11.5 本地接力测试清单

启动 Studio（`cd studio; npm start`）后逐项验证：

1. **模型切换器**
   - [ ] 顶栏 Provider 区点击打开，默认只展开当前模型所在供应商组
   - [ ] 搜索"gpt"/"claude"/"豆包"等能过滤；分类 chips 过滤正确
   - [ ] 点击模型 → 顶栏更新、✓ 标记正确；自定义 `provider:model` 回车可应用
2. **在线拉取**（需对应 Key 环境变量）
   - [ ] OpenAI 卡片 → 模型 → 「拉取在线模型」：应命中 `https://api.openai.com/v1/models`
   - [ ] Anthropic（无版本号 base_url）：应自动尝试 `/v1/models` 且带 x-api-key
   - [ ] 通义（/compatible-mode/v1）、豆包（/api/v3）、智谱（/v4）、Cohere（{models:[]} 格式）均能解析
   - [ ] 无 Key 时 401 显示"鉴权失败，请检查环境变量 XXX"
   - [ ] 勾选若干模型 → 添加 → 卡片模型区出现新标签 → 切换器中可选 → 重启 Studio 后仍在（settings 持久化）
3. **多端点测速**
   - [ ] 卡片「测速」→ 模态预填当前 base_url；添加非法 URL 有 toast
   - [ ] 添加 2-3 个端点（可试 OpenAI 官方/镜像）→ 开始测速 → 按延迟排序、最快行高亮
   - [ ] 「采用」→ 编辑弹窗 base_url 已替换、YAML 片段正确
   - [ ] 自定义端点可「移除」，种子端点无移除按钮
4. **回归**：供应商卡片「测试」延迟颜色；设置页 V7 组、配额模态、侧边栏状态行不受影响

### 11.6 未移植的 cc-switch 功能（评估后暂缓）

- **AutoFailoverConfigPanel 熔断器面板**：cc-switch 是本地代理（maxRetries/熔断阈值/恢复阈值/错误率），agentcrew V7 后端路由目前只有 weighted/rotate/usage/order，无熔断状态机；需先做后端再做 UI
- **卡片 ⋯ 更多菜单 / 复制供应商 / 打开终端**：agentcrew 卡片已有 测试/测速/编辑/模型 四按钮，复制供应商与 Python 包形态价值低
- **Claude 多槽位模型映射**（haiku/sonnet/opus/fable/subagent 分档）：agentcrew 用单一 model alias，架构不同
- **预设同厂商多版本合并成行 / 套餐×地区网格**：agentcrew 预设已按 key 平铺且带分类筛选，收益有限
