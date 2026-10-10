# AgentCrew 供应商与模型管理升级 — 工作提示词（V4）

> 本文档是给 AI 编码代理（ZCode / Claude Code 等）的完整工作提示词。目标：参考成熟开源项目
> **cc-switch** 的供应商管理设计，为 AgentCrew 补齐「多供应商接入 / 模型发现 / 连通性测速 /
> Studio 供应商管理页」能力，并修复分析中发现的两个 Studio 链路 bug。
>
> 参考源码位置：`E:\deepseek idea\cc-switch-main\cc-switch-main`（只读参考，**不得向该目录写入任何文件**）。
> 工作目录：`E:\agentcrew`。

---

## 一、背景：cc-switch 值得移植的设计（分析结论）

cc-switch（Tauri 2 + Rust + React）是 Claude Code / Codex / Gemini CLI 等工具的供应商配置管理器，
累计管理 10 种 Agent 工具的配置。其核心设计与 AgentCrew 的对应关系：

| # | cc-switch 设计 | 参考位置（cc-switch 源码） | 对 AgentCrew 的启示 |
|---|---|---|---|
| 1 | **Provider SSOT 数据模型**：`{id, name, settingsConfig, category, meta, icon, inFailoverQueue, sortIndex}`，SQLite DAO 存储，每个 app 一个「当前 provider」指针 | `src-tauri/src/provider.rs`、`src-tauri/src/database/dao/providers.rs` | AgentCrew 缺「用户自定义 provider」概念，`PROVIDER_PRESETS` 只有 9 个且不可扩展 |
| 2 | **预设供应商库**：几十个供应商预设，含 `settingsConfig` 模板、`endpointCandidates`（多端点候选）、`apiKeyUrl`、`apiFormat` 格式标记、family/plan/region 分类、搜索别名 | `src/config/*ProviderPresets.ts`、`presetFamilies.ts`、`presetSearchAliases.ts` | AgentCrew 内置预设太少（无 OpenRouter/SiliconFlow/Moonshot/GLM 等聚合与中转平台），无端点候选 |
| 3 | **模型发现**：`GET /v1/models`，URL 候选自动回退（`/v1/models` → `/models` → 剥离子路径变体），支持 `models_url` 精确覆写 | `src-tauri/src/commands/model_fetch.rs`、`src-tauri/src/services/model_fetch.rs` | AgentCrew 完全没有模型列表发现，用户只能手写模型名 |
| 4 | **连通性测速**：多端点延迟测试、流式检测（stream check） | `src-tauri/src/services/speedtest.rs`、`stream_check.rs` | AgentCrew 有 P2-11 健康监控（二值健康），无延迟量化，Studio 不可见 |
| 5 | **官方种子幂等注入**：`OFFICIAL_SEEDS` 固定 id，`ensure_official_seed_by_id` 保证「一键切回官方」永远存在 | `src-tauri/src/database/dao/providers_seed.rs` | 预设扩充时应保证内置预设幂等、可被用户覆盖而非删不掉 |
| 6 | **配置版本迁移 + 备份**：数据库 schema 版本化迁移、写前备份 | `src-tauri/src/database/migration.rs`、`backup.rs` | AgentCrew 的 `agentcrew.yaml` 无版本字段，未来演进无迁移机制 |
| 7 | **分层架构**：commands（薄命令层）→ services（业务）→ DAO（存储）→ live 引擎（写目标配置），层间单向依赖 | `src-tauri/src/commands/`、`services/` | AgentCrew 已有类似分层（api/ → models/ → config.py），新功能沿用 |
| 8 | **桌面端供应商管理页**：卡片列表 + 当前标记 + 一键切换 + 测速按钮 + 从预设添加 | `src/components/providers/` | AgentCrew Studio 8 页中没有供应商管理页；新建运行选模型用 12 个硬编码裸名 |

**重要差异 —— 有一处明确不要学**：cc-switch 把 API Key 明文存在自己的 config 里（本地单用户工具的取舍）。
AgentCrew 的安全模型是「密钥只从环境变量读取，绝不落盘」（`agentcrew/config.py` 的校验器、脱敏、
`secret_values()` 整套机制）。本次升级**必须保持** AgentCrew 的密钥安全模型：自定义 provider 只存
`api_key_env`（环境变量**名**），不存值。

---

## 二、AgentCrew 现状快照（改代码前必读）

已完成（不要重复造轮子，全部有测试覆盖）：

- 五级配置合并：代码默认 → `.env` → `agentcrew.yaml` → `AGENTCREW_*` 环境变量 → CLI（`agentcrew/config.py`）
- 9 个内置 provider 预设 + 能力位（`agentcrew/models/config.py` 的 `PROVIDER_PRESETS`，含实测注释如 deepseek 不支持 json_schema）
- 别名格式 `<provider>:<model>`，`parse_alias` 按第一个冒号切分
- `provider_overrides`（YAML 覆盖已存在 provider 的 base_url/capabilities，密钥字段直接报错）
- 故障转移 + 权重路由（`model_failover`，`agentcrew/models/factory.py`）
- 健康监控（P2-11：后台 30s 探测 + `HealthRecord`，failover 健康感知）
- 成本上限、LLM 缓存、复杂度分层选模型、doctor 诊断（含 provider 连通性）
- API server（`agentcrew/api/server.py`）：runs CRUD、`PUT /runs/{id}/model` 运行中切模型、SSE、ledger/stats、API Key 认证 + 限流
- Studio（Electron）：8 页导航、SSE 实时、运行对比、成本分析、导出报告、e2e 15 项检查（`studio/e2e.js`）

分析中发现的两处 **Studio 链路 bug**（本次必须修复）：

1. `studio/renderer.js:1589` 的 `COMMON_MODELS` 是裸模型名（`'gpt-4o'`、`'deepseek-chat'`…），
   而引擎要求别名必须带 provider 前缀（`parse_alias` 无冒号即报「模型别名缺少 ':'」）。
   设置页选出的「默认模型」保存后根本无法被引擎解析。
2. `studio/main.js:543-550` 构造运行参数时**从不传 `--model`**，设置页保存的默认模型
   对新建运行完全不生效（只有运行中切模型的 `model:switch` IPC 链路是通的）；
   设置页的 `maxCostUsd` 同样没接入新建运行链路（只有弹窗显式填写才传）。

已知缺口（与 cc-switch 对比）：

- 无法新增自定义 provider：`Settings._validate_provider_overrides` 对未知 provider 直接报错
  （`agentcrew/config.py:462`）。用中转站 / 自建网关的用户只能整体覆盖 `openai` 的 base_url，
  官方与中转无法并存。
- 无模型发现（`GET /v1/models`）、无端点候选、无延迟测速。
- Studio 无供应商管理页，设置页仅 5 个字段，模型选择体验断裂。
- `agentcrew.yaml` 无版本字段、无写前备份、无导入导出。

---

## 三、红线（违反任何一条即返工）

1. **密钥绝不落盘**：任何新增配置字段沿用「只存环境变量名」模式；`config_snapshot()` 落盘前
   对新字段的脱敏必须同步扩展（参考 `_redact_unknown` / `redact_overrides` 的按名判密钥递归）；
   测试要钉住「密钥字段写进新配置 → 报错」「旧 run.json 密钥 → 快照脱敏」两条路径。
2. **确定性引擎不变**：调度决策零 LLM 参与的原则不动；不引入任何常驻网络轮询到引擎主循环
   （测速/模型发现都是按需调用，健康监控沿用 P2-11 的后台任务模式）。
3. **配置合并顺序不变**：`代码默认 → .env → agentcrew.yaml → 环境变量 → CLI`；新字段必须
   同时接入 `_yaml_values` / `_env_values`（带 `AGENTCREW_` 前缀环境变量）与 CLI 覆盖。
4. **向后兼容**：现有 `agentcrew.yaml`、`PUT /runs/{id}/model`、既有 API 响应结构不破坏；
   9 个内置预设的 key 与语义不变（用户 run.json 快照里有它们）。
5. **测试守卫不删**：`PROJECT-STATE.md`「改代码前必读」列出的守卫（死锁结构性检查、
   `bump_rework` 唯一调用点等）原样保留；全量 `pytest` 必须全绿（当前基线 1632 passed / 1 skipped）。
6. **Studio e2e 既有 15 项检查保持通过**；新增页面后 `studio/e2e.js` 同步扩展。
7. **不修改 cc-switch 参考目录**；Studio 零新增 npm 重型依赖（沿用现有 vanilla JS + h() DOM 构建模式）。

---

## 四、任务清单（按序实施，每项独立可验收）

### 阶段 A：引擎侧（Python）

**A1. 自定义 Provider 注册表**（核心，最先做）

- `agentcrew.yaml` 新增 `models.custom_providers:` 段：`名字 → {implementation, base_url, api_key_env, capabilities?, command?, suggested_models?}`；
  环境变量 `AGENTCREW_CUSTOM_PROVIDERS` 支持紧凑 JSON 形式。
- `Settings` 新增 `custom_providers` 字段，进 `_MAPPING_FIELDS`、`config_snapshot()`（脱敏）。
- `models/config.py` 新增 `custom_providers(overrides=None)` 视图函数：内置预设 + 用户自定义合并后的
  只读目录；`parse_alias` 与 `ModelFactory` 改为查合并视图。自定义名与内置重名时报错（不允许覆盖内置，
  想覆盖走既有 `provider_overrides`）。
- 校验沿用 `_check_override_fields` 的 schema 递归思路：未知字段、疑似密钥字段一律报错。
- CLI：`python main.py providers [name]` 列出合并目录（预设/自定义标记、base_url、api_key_env、能力位摘要）。
- 测试：新增/校验/脱敏/重名/别名解析/未知 provider 报错全路径。

**A2. 预设库扩充**

- 在 `PROVIDER_PRESETS` 增补主流聚合与国内平台（至少：`openrouter`、`siliconflow`、`moonshot`、
  `zhipu`（GLM）、`groq`、`together`），每个含实测风格的能力位注释、`suggested_models` 建议列表
  （新字段，A1 一并加进 `ProviderPreset`）。
- 学 cc-switch 的 `providers_seed.rs`：内置预设幂等——用户可覆盖不可删除，目录里始终可见。
- base_url 以官方文档为准，不确定的在注释里标注「未实测」；**不要**抄 cc-switch 里带推广链接的
  第三方中转站（那是它的商业模式，不是我们的）。

**A3. 模型发现服务**

- 新增 `agentcrew/models/discovery.py`：`async fetch_models(binding) -> list[str]`，OpenAI 兼容
  `GET {base_url}/models`；URL 候选回退学 cc-switch（`/v1/models` → `/models` → 剥离子路径变体），
  支持 `models_url` 覆写；超时（10s）与错误分类（网络/鉴权/404/解析）；结果排序去重。
  anthropic 实现（`/v1/models`）单独适配。
- 复用现有 Provider 健康探测的 HTTP 栈风格；不新增依赖。
- CLI：`python main.py models <provider>`（列模型，`--json` 输出）；`doctor` 集成（显示每个已配置
  provider 的模型可发现性）。
- 测试：mock HTTP（参考 `test_openai_compat.py` / `test_provider_health.py` 的做法）覆盖候选回退、
  超时、404、鉴权失败、去重排序。

**A4. 测速与健康整合**

- `HealthRecord` 增加最近一次探测延迟（ms）与时间戳（保持向后兼容：缺省 None）。
- 新增按需测速：对 provider 发一次最小补全请求（`max_tokens=1`）记录延迟；一次只能测一个 provider，
  不得并发轰炸。
- API：`POST /providers/{name}/test` → `{ok, latency_ms, error?}`；`GET /providers` 返回目录 +
  健康状态 + 延迟。沿用既有认证/限流中间件。

**A5. API 端点：供应商目录**

- `GET /providers`：合并目录（内置/自定义标记、base_url、api_key_env 名、capabilities 摘要、
  健康状态、suggested_models）。
- `GET /providers/{name}/models`：调 A3 实时拉取（带错误透传与缓存 60s，进程内即可）。
- 响应绝不包含密钥值（只有 `api_key_env` 名字与 `key_configured: bool`）。
- 测试进 `tests/test_api_server.py`。

**A6. 配置导入导出与版本化**

- `python main.py config export > agentcrew.export.yaml`：导出合并视图（密钥字段天然不存在；
  `provider_overrides`/`custom_providers`/`mcp_servers` 走脱敏函数）。
- `python main.py config import <file>`：校验后合并写入 `agentcrew.yaml`；**写前备份**为
  `agentcrew.yaml.bak`（学 cc-switch backup 思路，但单文件足够）。
- `agentcrew.yaml` 顶层支持 `config_version`（当前 1）：加载时发现更高版本报清晰错误，为未来
  迁移留钩子（不必实现具体迁移）。

### 阶段 B：Studio 侧（Electron）

**B1. 修复两条链路 bug（先行，独立于新页面）**

- `COMMON_MODELS` 改为 `['<provider>:<model>', ...]` 结构化列表（provider + model 两段），
  或直接改为 B2 的「provider 下拉 + 动态模型」组件后删除该常量。
- `studio/main.js` 构造运行参数：设置页 `defaultModel` 传 `--model`（用户在新建弹窗里显式选择时
  以弹窗为准）；`maxCostUsd > 0` 时传 `--max-cost`。空值行为与 CLI 现有约定一致。

**B2. 新建运行弹窗：模型选择改造**

- provider 下拉（来自 `GET /providers`，离线/无后端时回落内置常量）→ 模型下拉（来自
  `GET /providers/{name}/models`，失败回落 `suggested_models` 与手输）→ 组装成
  `<provider>:<model>` 别名显示在确认区。
- 后端不可用时全部控件可手输，不阻塞新建运行（保持 Studio 现有降级风格）。

**B3. 新增第 9 页「供应商」（导航 data-nav="providers"）**

- 卡片列表：名称、base_url、内置/自定义徽标、api_key_env 与「已配置 ✓ / 未配置」、
  健康状态点（复用顶部连接指示器的视觉语言）、最近延迟。
- 卡片操作：测试（调 `POST /providers/{name}/test`，按钮态与结果内联展示）、拉取模型
  （结果以列表/标签展示）。
- 「从预设添加」弹窗：选预设 → 填 base_url（预填）/ api_key_env（环境变量名，明示「请把真实
  Key 写进 .env 或系统环境变量」）→ 生成 `models.custom_providers:` YAML 片段展示给用户复制
  （Studio 不直接改 agentcrew.yaml，保持只读仓库文件的现状；弹窗里给出一键复制与写法说明）。
- 快捷键提示「1-9」同步更新；e2e 增加：第 9 页导航可达、卡片渲染、测试按钮存在。

**B4. e2e / selftest 同步**

- `studio/e2e.js`：新增供应商页检查（导航、卡片、弹窗开关）；`studio/selftest.js` 若覆盖导航
  枚举则同步。

### 阶段 C：可选增强（时间富余再做，不阻塞验收）

- C1. Electron 单实例锁（`app.requestSingleInstanceLock`）+ 关闭到托盘 + 托盘菜单（打开主窗/退出）。
- C2. doctor 输出增加「模型发现」检查项；`python main.py providers --json` 供外部工具消费。

---

## 五、验收标准（全部满足才算完成）

1. `python -m pytest -q` 全绿（基线 1632 passed / 1 skipped，只增不减）。
2. `cd studio && npm test && npm run e2e` 全绿（既有 15 项 + 新增项）。
3. `ruff check . && mypy agentcrew`（沿用 CI 口径）零错误。
4. 功能演示（mock 即可，不烧真实 token）：
   - `agentcrew.yaml` 写一个 `custom_providers: {myrelay: {implementation: openai_compat, base_url: http://localhost:9/v1, api_key_env: MYRELAY_KEY}}` → `python main.py providers` 能列出、`parse_alias('myrelay:x')` 能解析、写成别名后 `--mock` 运行不报未知 provider；
   - `python main.py models openai` 对不可达端点给出分类清晰的错误（不栈崩溃）；
   - `python main.py config export | python main.py config import -`（或临时文件）往返不丢字段，密钥字段即使手混进导出文件也会被拒/脱敏。
5. 密钥安全静态自查：新增代码路径中不出现把 `os.environ[<key值>]` 写入任何文件/响应/日志的语句；
   `GET /providers` 响应中有测试断言不含任何 `os.environ` 里的密钥值。
6. Studio 手工链路：设置页选默认模型 → 新建运行 → 进程参数里出现 `--model <provider>:<model>`。

---

## 六、工作方式约定

- 先读 `PROJECT-STATE.md` 与 `agentcrew/config.py`、`agentcrew/models/config.py`、
  `agentcrew/models/factory.py`、`agentcrew/api/server.py`、`studio/main.js`、`studio/renderer.js`，
  再动手；本提示词中引用的 cc-switch 文件按需查阅，不整目录通读。
- 测试先行：每个任务先写失败测试再实现；沿用现有测试文件命名（`test_config.py`、
  `test_model_config.py`、`test_api_server.py` 等）就近追加。
- 小步提交：每个任务一个 commit，格式沿用仓库惯例（`feat(models): ...` / `fix(studio): ...`），
  中文描述。
- 遇到与「红线」冲突的实现捷径，宁可功能少做也不破红线；拿不准就在 commit message 与
  `PROJECT-STATE.md` 里如实记录取舍。
- 完成后更新 `PROJECT-STATE.md`（新增 V4 小节）与 `README.md`（供应商管理相关段落）。
