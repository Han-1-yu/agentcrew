# V4 供应商与模型管理升级 — 质量审查与整改报告

审查时间：2026-10-10
审查范围：引擎侧 A1-A6 + Studio 侧 B1-B4（已完成，可修改）；V4-FE F0-F3（只读，不修改）
审查模式：系统性多维度审查 + 发现问题立即修复 + 全量回归验证

---

## 一、审查维度

| # | 维度 | 说明 |
|---|------|------|
| 1 | 红线合规 | 密钥不落盘、配置合并顺序、向后兼容、确定性引擎 |
| 2 | 测试质量 | 全量测试通过、新增测试覆盖度、无 skip/xfail 掩盖 |
| 3 | 代码质量 | ruff / mypy 零新增错误、异常处理、无硬编码魔法值、无重复代码 |
| 4 | 功能完整性 | 对照 A1-A6 / B1-B4 任务清单逐项核对 |
| 5 | 文档一致性 | PROJECT-STATE.md / README.md 与实现一致 |

---

## 二、发现的问题清单与整改措施

### 引擎侧（A1-A6）— 发现并修复 5 项

#### E-01 [P0 功能 bug] A2 新增平台 api_key_env 未同步硬编码映射

- **文件**：`agentcrew/config.py:594`（`api_key_for` 方法）
- **问题**：A2 为 openrouter/siliconflow/moonshot/zhipu/groq/together 六个新预设设置了 `api_key_env`，但 `api_key_for()` 只查硬编码的 `_PROVIDER_KEY_ENV`（原始 6 家）。用户即便设了对应环境变量，`api_key_for()` 仍返回 None，工厂误报「缺少 API Key」——**新平台完全无法发 LLM 调用**。同时 `secret_values()` 也漏了新预设与自定义 provider 的密钥值（日志脱敏不全）。
- **根因**：两份会漂移的映射（PROVIDER_PRESETS 中的 api_key_env vs _PROVIDER_KEY_ENV 硬编码）未保持同步。
- **修复**：新增 `_api_key_env_name()` 从合并目录（overrides→内置预设→自定义）取环境变量名，消除硬编码映射；`secret_values()` 遍历全部预设+自定义。补回归测试。
- **Commit**：`89cea50` `fix(engine): A2 新增内置平台 api_key_env 未同步硬编码映射导致永远缺 Key`

#### E-02 [UX bug] 模型发现错误结果被缓存 60s 无法即时重试

- **文件**：`agentcrew/api/server.py:889`（`_discover_models`）
- **问题**：模型发现把 `ModelDiscoveryError` 也写进 60s 缓存，瞬时网络抖动/鉴权失败后，用户 60s 内点「刷新模型」仍看到陈旧错误、无法即时重试。
- **修复**：只缓存成功结果；错误实时透传，下次请求立即重试。补测试钉住两次请求都真发 HTTP 调用。
- **Commit**：`f5f6414` `fix(engine): 模型发现错误结果不再缓存 60s，允许即时重试`

#### E-03 [A1 完整性缺口] doctor 未识别自定义 provider 别名

- **文件**：`agentcrew/cli/doctor.py:165,195`
- **问题**：doctor 两处 `parse_alias` 只传 `overrides=`、漏传 `custom=`，导致 `default_model` 指向自定义 provider 时被误报「别名非法」，且其 API Key 检查被跳过。
- **修复**：两处都补 `custom=settings.custom_providers`。补回归测试（default_model 指向自定义 provider 时 doctor 不再误报）。
- **Commit**：`53b8ca6` `fix(engine): doctor 识别自定义 provider 别名与 Key`

#### E-04 [健壮性] anthropic 预设缺显式 models_url

- **文件**：`agentcrew/models/config.py:77`（anthropic 预设）
- **问题**：anthropic base_url 不带 `/v1`，discovery 先试 `/models`；而 `fetch_models` 对 parse 错误不回退（换 URL 也不试）。若 `/models` 回 200 HTML 而非 404，模型发现会误报 parse 错误。
- **修复**：显式设 `models_url="https://api.anthropic.com/v1/models"` 固定到正确端点。补断言。
- **Commit**：`b12ed7e` `fix(engine): anthropic 预设显式 models_url 指向 /v1/models`

#### E-05 [代码质量] 修改文件中预存 ruff 错误清理

- **文件**：`agentcrew/api/server.py:523`、`agentcrew/cli/doctor.py:101`
- **问题**：
  - B010：`setattr(crew, "_on_event", forward)` 应用直接赋值（预存，源自 P1-5 提交）
  - UP036：`if sys.version_info >= (3, 10)` 版本检查过时（包元数据已要求 >= 3.10）
- **修复**：setattr 改 `crew._on_event = forward`；移除过时版本检查分支，直接报告当前版本。
- **Commit**：`77b7706` `fix(engine): 清理修改文件中预存 ruff 错误——B010 setattr 改直接赋值、UP036 过时版本检查`

### Studio 侧（B1-B4）— 发现并修复 1 项

#### S-01 [一致性缺陷] 侧边栏导航顺序与快捷键说明不一致

- **文件**：`studio/index.html:103-110`、`studio/renderer.js:1362`
- **问题**：侧边栏视觉顺序为 …事件(7)/供应商(8)/设置(9)，但键盘 `pages` 数组为 …events(7)/**settings(8)/providers(9)**，且 e2e 第14项已锁定"按 9 切供应商"。设置页"关于"里的快捷键说明写成"…事件/供应商/设置"，与真实键位相反——用户按 8 期望供应商却进到设置。
- **根因**：B3 把 providers 插在 settings 之前，B4 却把 providers 追加到键盘数组末尾（key9）。
- **修复**：把 providers 导航块移到 settings 之后（视觉第9=供应商）；"关于"快捷键说明改为"…事件/设置/供应商"。
- **Commit**：`fcb962e` `fix(studio): 对齐侧边栏导航顺序与快捷键说明——第8=设置、第9=供应商`

### V4-FE 只读审查（F0-F3）— 发现 8 项，未修改代码

详细清单见 `docs/v4-fe-review-findings.md`。摘要：

| 编号 | 严重度 | 要点 |
|------|--------|------|
| FE-001 | 高 | DAG 节点主标题 `fill:#1C1C1E` 压在深色节点框上，默认深色主题下核心任务图标题不可读 |
| FE-002 | 中 | 状态/角色/事件色硬编码浅色 hex，不随主题切换；styles.css 全套 `--status-*` token 成死变量 |
| FE-003 | 中 | 三处内联 input 边框 `rgba(60,60,67,.18)` 深色模式下几乎消失 |
| FE-004 | 低 | 启动时浅色闪烁（data-theme 设置晚于首屏渲染） |
| FE-005 | 低 | saveSettings 冗余推送 `theme:changed` 事件 |
| FE-006 | 低 | 产物图标 SVG 重复 path 越界 |
| FE-007 | 低 | renderer.js 中 `C` 对象部分字段为死代码 |
| FE-008 | 低 | cycleTheme 持久化失败时状态不一致 |

---

## 三、审查确认无问题的项

### 红线合规
- **密钥绝不落盘**：custom_providers 只存 `api_key_env` 环境变量名，不存密钥值；`config_snapshot()` 对 `provider_overrides` 与 `custom_providers` 均按 schema 递归脱敏；`GET /providers` 响应只返回 env 名 + `key_configured` bool（有测试钉住 `"sk-supersecret"` 不出现在响应体）；config import 拒绝含 `api_key` 密钥值的配置并报清晰错误。
- **配置合并顺序**：custom_providers 同时接入 `_yaml_values`（agentcrew.yaml）、`_env_values`（AGENTCREW_CUSTOM_PROVIDERS JSON）、CLI 覆盖。
- **向后兼容**：原 9 个预设 key/语义不变；现有 agentcrew.yaml 照常加载；`PUT /runs/{id}/model` 与既有 API 结构未破坏；`HealthRecord.latency_ms` 缺省 None 保兼容。
- **确定性引擎**：无 LLM 参与调度，无常驻网络轮询进主循环。

### 功能完整性（逐项核对通过）
- **A1**：custom_providers 段、Settings 字段、_MAPPING_FIELDS、config_snapshot 脱敏、build_directory 合并视图、parse_alias/ModelFactory 查合并目录、重名报错、CLI providers — 全部实现并测试覆盖。
- **A2**：6 个新预设（openrouter/siliconflow/moonshot/zhipu/groq/together）、能力位注释、suggested_models、幂等性、base_url 准确 — 全部实现。
- **A3**：discovery.py、fetch_models、URL 候选回退（/v1/models→/models→剥离子路径）、models_url 覆写、10s 超时、错误分类（auth/not_found/network/timeout/parse）、排序去重、anthropic 适配、CLI models、mock HTTP 测试 — 全部实现。
- **A4**：HealthRecord 延迟字段（缺省 None）、按需测速 max_tokens=1、POST /providers/{name}/test、GET /providers 含延迟 — 全部实现。
- **A5**：GET /providers 响应结构、GET /providers/{name}/models 实时拉取+60s 缓存+错误透传、响应不含密钥值、key_configured bool — 全部实现。
- **A6**：config export（脱敏）、config import（校验+写前备份 .bak）、config_version 顶层字段（更高版本报清晰错误）— 全部实现。
- **B1**：COMMON_MODELS 结构化、main.js 传 --model（弹窗优先→settings 回落）、--max-cost — 全部实现。
- **B2**：新建运行弹窗 provider 下拉→模型下拉→别名展示、后端不可用手输降级、mock 优先 — 全部实现。
- **B3**：第9页导航、卡片列表、测试按钮、拉模型、从预设添加弹窗（YAML 片段+复制）、快捷键 1-9、三级降级 — 全部实现。
- **B4**：e2e 新增项覆盖 B1-B3 功能 — 全部实现。

### 测试质量
- 新增测试均为真实断言（非空转），覆盖 custom_providers 校验/脱敏/重名/别名解析、discovery 候选回退/超时/404/鉴权失败/去重排序、API 端点响应结构/密钥不含/缓存、config export/import 往返/密钥拒绝。
- 无 skip/xfail 掩盖问题。
- V4 测试文件：test_model_discovery.py、test_model_config.py、test_config_io.py、test_api_server.py、test_provider_health.py。

### 文档一致性
- PROJECT-STATE.md V4 小节准确完整，A1-A6/B1-B4 逐项列出。
- README.md V4 增强功能段落（第 957 行起）与实现一致，涵盖自定义 provider、模型发现、测速、API 端点、配置导入导出、Studio 供应商页。

---

## 四、验证结果

### 整改后全量验证（全部通过）

| # | 验证项 | 结果 |
|---|--------|------|
| 1 | `python -m pytest -q` | **1686 passed, 1 skipped**（基线 1682 + 新增 4 个回归测试） |
| 2 | `cd studio && npm run e2e` | **allPass = True**（全部通过） |
| 3 | `ruff check`（V4 修改文件） | **All checks passed!**（零错误） |
| 4 | `mypy`（V4 新增/修改文件） | **零新增错误**（discovery.py/doctor.py/config.py 零错误；config.py:289、factory.py 多处为预存错误，非 V4 引入） |
| 5 | 密钥安全静态自查 | **通过**（无 os.environ 值写入文件/响应/日志；custom_providers 只存 env 名；config_snapshot 脱敏；GET /providers 不含密钥值；config import 拒绝密钥值） |

### 功能演示验证（mock 不烧真实 token，全部通过）

| # | 演示项 | 结果 |
|---|--------|------|
| 1 | custom_providers 配置 → providers 列出 → parse_alias 解析 | **通过**：myrelay 显示为 `[custom]`，parse_alias 返回 `provider=myrelay, model=relay-model, impl=openai_compat` |
| 2 | doctor 识别自定义 provider 别名 | **通过**：default_model 可解析，无"别名非法"误报（E-03 修复后） |
| 3 | models openai 对不可达端点 | **通过**：返回清晰 `[timeout] openai 模型发现失败：请求超时`，无栈崩溃 |
| 4 | config export 脱敏 | **通过**：`api_key_env: MYRELAY_KEY` 只存环境变量名，不含密钥值；`config_version: 1` 顶层字段存在 |
| 5 | config import 拒绝密钥值 | **通过**：含 `api_key: sk-leaked-value` 的配置被拒，报错"API Key 只从环境变量读取，请改用 api_key_env 指定环境变量名" |
| 6 | config import 合法往返 | **通过**：字段不丢，写前自动创建 `.bak` 备份 |

---

## 五、整改 Commit 清单

| Commit | 类型 | 说明 |
|--------|------|------|
| `89cea50` | fix(engine) | A2 新增平台 api_key_env 未同步硬编码映射（P0） |
| `f5f6414` | fix(engine) | 模型发现错误结果不再缓存 60s |
| `53b8ca6` | fix(engine) | doctor 识别自定义 provider 别名与 Key |
| `b12ed7e` | fix(engine) | anthropic 预设显式 models_url |
| `77b7706` | fix(engine) | 清理修改文件中预存 ruff 错误（B010+UP036） |
| `fcb962e` | fix(studio) | 对齐侧边栏导航顺序与快捷键说明 |

---

## 六、遗留事项

1. **V4-FE 问题**：8 个 F0-F3 问题（含 1 个高优先级 DAG 深色可读性）已记录在 `docs/v4-fe-review-findings.md`，因 V4-FE 仍在进行中（另一个 OrganizerAgent 负责），本次审查未修改代码，建议 V4-FE 负责人优先处理 FE-001（DAG 深色模式标题不可读）。
2. **预存 mypy 错误**：`config.py:289`（E-3 api_keys 类型窄化）、`factory.py` 多处（LLMCache None 解引用、BaseException vs Exception）为 V4 之前的预存错误，本次未处理，建议后续单独清理。
3. **e2e 第15项**：计划审查页硬编码 `check(..., true, ...)` 恒通过，属预存问题（非 B1-B4 新增），建议后续加固。

---

## 七、结论

V4 供应商与模型管理升级（引擎侧 A1-A6 + Studio 侧 B1-B4）经系统性审查，共发现 **6 个问题**（引擎 5 + Studio 1），其中 1 个 P0 级功能 bug（A2 新平台完全无法发 LLM 调用），已全部修复并提交。修复后全量测试 **1686 passed / 1 skipped**，Studio e2e 全绿，ruff/mypy 零新增错误，密钥安全静态自查通过，6 项功能演示全部通过。V4-FE F0-F3 只读审查发现 8 个问题（高 1 / 中 2 / 低 5），已记录待 V4-FE 负责人处理。
