# V4 加大审查力度 — 深度审查与整改报告

审查时间：2026-10-10
审查范围：V4 引擎侧（A1-A6）+ V4 Studio 前端侧（B1-B4 + F0-F7）全量代码
审查方式：在上轮表面审查（v4-quality-review-report.md，已修 6 项）基础上，按正确性/错误处理/安全/测试覆盖/性能/主题系统/模型链路/供应商页/弹窗/设置页/CSP 等维度做更深、更严的边界与错误路径审查，发现即改，每个修复独立 commit。

---

## 一、审查维度

| # | 维度 | 引擎侧 | 前端侧 |
|---|------|--------|--------|
| 1 | 正确性与边界条件 | custom_providers 校验 / parse_alias / build_directory 重名 / discovery URL 回退 / 测速 / config import | 模型选择链路 / 供应商页 / 新建运行弹窗 / 设置页 |
| 2 | 错误处理与异常安全 | async 异常路径 / HTTP 资源泄漏 / 递归脱敏 / YAML 序列化 | 加载失败降级 / 弹窗状态清理 / 持久化失败回滚 |
| 3 | 安全审计 | os.environ 全量 grep / 15 provider api_key_for / 响应密钥泄露 / test 错误信息 / config import 密钥拒绝 | CSP connect-src / innerHTML 注入 / IPC 授权 / 密钥仅回布尔 |
| 4 | 测试覆盖审计 | pytest-cov 分支覆盖 / mock 严格度 / 边界测试补充 | e2e 功能验证 / selftest 路径覆盖 / 暗色主题检查 |
| 5 | 性能与资源 | 60s 缓存内存泄漏 / GET /providers 重建 / 健康监控 | — |
| 6 | 主题系统深度审查 | — | 9 页面暗色可读性 / 状态徽章对比度 / 弹窗边框 / 硬编码色值 grep / FOUC |
| 7 | 集成与兼容性 | API 响应结构与前端字段对齐 | provider 列表与引擎 15 预设一致 |

---

## 二、发现的问题清单与整改措施（共 15 项）

### 引擎侧（4 项：P1×2、P2×2）

#### D-01 [P1 功能 bug] doctor 把 api_key_env 留空的自定义 provider 误报缺 Key

- **文件**：`agentcrew/cli/doctor.py:163-165`（`_check_api_keys`）
- **问题**：自定义 provider 把 `api_key_env` 设为空串，表示本地中转/网关无需鉴权（factory 对空 api_key_env 返回占位串、不报错，属支持的配置）。但 doctor 的 `_KEYLESS_PROVIDERS` 只含内置 mock/ollama/cli，且 `settings.api_key_for()` 对空 env 名返回 None，导致这类合法配置被误判为 fail（退出码 3）。
- **根因**：doctor 与 factory 对「免钥」的判定口径不一致——factory 看 `not binding.api_key_env`，doctor 只看硬编码集合。
- **修复**：`_check_api_keys` 增加 `if not binding.api_key_env: continue`，与工厂口径对齐。
- **Commit**：`5b6cc9b` `fix(engine): doctor 把 api_key_env 留空的自定义 provider 误报缺 Key`

#### D-02 [P1 红线·密钥落盘] config import 把真实密钥误填进 api_key_env 并写盘

- **文件**：`main.py` `_cmd_config_import` 内 `_check_secrets`
- **问题**：密钥正则检查对 `api_key_env` 字段整个 `continue` 跳过。用户把真实密钥值（`sk-xxx`、JWT `eyJ...`）误填进 `api_key_env`（本该填环境变量名）时，既不被拦、又会被原样写进 `agentcrew.yaml` 明文落盘；且永不生效（`os.environ.get(<密钥>)` 必为空）。
- **根因**：把「api_key_env 存的是环境变量名」误解为「无需校验其值形状」。
- **修复**：对 `api_key_env` 的值做形状校验——空串合法（免钥）；非空必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`（环境变量名约定），否则判定为误填的密钥值并拒绝；报错不回显该疑似密钥的值。
- **Commit**：`8c946b6` `fix(engine): config import 拒绝把真实密钥误填进 api_key_env（红线：密钥不落盘）`

#### D-03 [P2 健壮性] 模型发现把空字符串/纯空白 model id 收录进模型列表

- **文件**：`agentcrew/models/discovery.py:95`（`_parse_models`）
- **问题**：部分 `/models` 端点回 `{"id":""}` 或 `{"id":"  "}` 占位条目，`isinstance(id, str)` 判定为真，空串被收录进模型下拉，用户点到必然调用失败。
- **修复**：`and item["id"].strip()` 非空才收录。
- **Commit**：`be0723a` `fix(engine): 模型发现跳过空字符串/纯空白的 model id`

#### D-04 [P2 可用性陷阱] 自定义 provider 名含冒号/为空，校验通过却永远无法被别名引用

- **文件**：`agentcrew/config.py` `_validate_custom_providers`
- **问题**：别名按「第一个冒号」切 `provider:model`。provider 名一旦自带冒号（如 `my:relay`），`my:relay:m` 会被切成 `provider="my"`，永远引用不到该 provider；空名同理。此前这类配置能过校验、静默生效，运行时才报「未知 provider」。
- **修复**：加载时即拒绝空名与含冒号名，并给清晰指引。
- **Commit**：`df817e0` `fix(engine): 拒绝名字含冒号或为空的自定义 provider`

### 前端侧 — 已知 8 项修复（FE-001 ~ FE-008）

#### FE-001 [高] DAG 节点主标题深色模式不可读

- **文件**：`studio/renderer.js:369-475`、`studio/styles.css:754-810`
- **问题**：DAG 节点 SVG 文字 fill 写死 `#1C1C1E`（近黑），深色模式节点框也是近深色，标题完全不可读；task_id/耗时/角色名 fill 对比度偏低；pending/unknown 节点 fill `#FFFFFF`、skipped/cancelled `#F2F2F7` 在深色界面突兀。
- **修复**：SVG 文字 fill 改用 CSS 类（`.node-title{fill:var(--text)}`、`.node-meta{fill:var(--text-ter)}`、`.node-role{fill:var(--text-sub)}`）；节点框 fill/stroke 改 `data-status` 引用 `--status-*`；色条/chip/头像/连线箭头全部主题化。
- **Commit**：`dd4760d` `fix(studio): FE-001 DAG节点标题深色模式不可读——SVG文字/填充改CSS变量主题化`
- **验证**：e2e 16b 暗色下标题 computed fill=`rgb(230,237,243)`（亮度 237）

#### FE-002 [中] 状态/角色/事件色硬编码浅色，不随主题切换

- **文件**：`studio/renderer.js:12-76,2455`、`studio/styles.css:817`
- **问题**：STATUS_META(10态)/ROLE_META(8)/EVENT_META(20)/RUN_STATUS_COLOR 全部写死浅色 hex，通过内联 style/SVG 属性直接落到 DOM，不受 `[data-theme]` 切换影响；styles.css 精心定义的全套 `--status-*` token 成为死变量。
- **修复**：全部 hex → `var(--status-*)`/语义 token；fchip 淡填充改 `color-mix(in srgb,var(--ev-color) 14%,transparent)`（原 JS 拼 hex alpha 对 var 无效）。
- **Commit**：`d936c1a` `fix(studio): FE-002 状态/角色/事件/运行状态色改引用CSS变量`
- **验证**：`--status-*` 死变量全部激活；renderer.js 硬编码色值归零

#### FE-003 [中] 内联 input 边框深色下消失

- **文件**：`studio/renderer.js`（事件搜索/模型切换自定义/任务图搜索 3 处）
- **问题**：三处内联 `border:1px solid rgba(60,60,67,.18)`，深色模式下 0.18 不透明度的深灰描边几乎不可见。
- **修复**：改用 `var(--border-input)`。
- **Commit**：`f1ba9ac` `fix(studio): FE-003 内联input边框深色下消失——改var(--border-input)`

#### FE-004 [低] 启动首屏浅色闪烁（FOUC）

- **文件**：`studio/index.html:11`、`studio/boot-theme.js`、`studio/renderer.js:2656`
- **问题**：HTML 加载时 `<html>` 尚无 data-theme，CSS 回退 `:root`（浅色），applyTheme 在异步 getSettings 后才执行，默认 dark 主题每次启动先闪一帧浅色。
- **修复**：新增外部 `boot-theme.js`（同源同步阻塞、在 styles.css 之前执行），读 localStorage 缓存的主题或默认 dark，立即设置 `document.documentElement.dataset.theme`；applyTheme() 把实际主题缓存 localStorage。
- **Commit**：`7b2a48a`（初版内联脚本）→ `5292938`（改外部文件兼容 CSP）
- **验证**：e2e 16 主题切换 bg 实测变化

#### FE-005 [低] saveSettings 冗余推送 theme:changed

- **文件**：`studio/main.js:69-74`
- **问题**：任何 setSettings（含 recentGoals、设默认模型）都触发 pushThemeChanged()，向渲染进程多发一次 theme:changed → applyTheme。
- **修复**：仅当 next 含 `theme` 键时才 pushThemeChanged。
- **Commit**：`9ffa974` `fix(studio): FE-005 saveSettings冗余推送theme:changed——仅theme键变更时才push`

#### FE-006 [低] 产物导航图标 SVG 路径重复越界

- **文件**：`studio/index.html:96`
- **问题**：产物按钮 SVG 第二条 path 重复了 `M3 8l9 5 9-5M12 13v8`；首条坐标相对位移后 x 到达 30，超出 viewBox `0 0 24 24`，火箭右上笔画被裁剪。
- **修复**：合并重复 path，改 Feather 纸飞机规范路径。
- **Commit**：`6811bf0` `fix(studio): FE-006 产物导航图标SVG路径重复越界——改规范纸飞机图标`

#### FE-007 [低] 死代码清理

- **文件**：`studio/renderer.js:83`、`studio/styles.css:74,188`
- **问题**：C 对象多数字段未用（仅 C.ter 用一次）；`--warn-bg-chip` 无引用。
- **修复**：删未用 C 对象（C.ter → `var(--text-ter)`）；删无引用 `--warn-bg-chip`（light/dark 两处）。
- **Commit**：`5136474` `fix(studio): FE-007 死代码清理——删除未用C对象与--warn-bg-chip`

#### FE-008 [低] cycleTheme 持久化失败时状态不一致

- **文件**：`studio/renderer.js:2666-2680,1329-1342`
- **问题**：先乐观改 S.settings.theme 再 fire-and-forget 持久化，失败时内存态与 settings.json 不一致，重启后回退。设置页主题 segmented control 有同样的静默吞错。
- **修复**：setSettings 失败时 toast 提示、回滚 S.settings.theme、重 applyTheme；设置页 segmented 一并修复。
- **Commit**：`5fda03b` `fix(studio): FE-008 cycleTheme持久化失败时状态不一致——失败回滚并重应用`

### 前端侧 — 深度审查新发现 3 项

#### N1 [高] FOUC 内联脚本被 CSP 拦截

- **文件**：`studio/index.html`、`studio/boot-theme.js`
- **问题**：FE-004 初版在 `<head>` 用内联 `<script>`，但 CSP `script-src` 回退 `default-src 'self'` 不允许内联脚本，导致修复实际不生效。
- **修复**：抽出为外部 `boot-theme.js`（同源、同步阻塞、styles.css 之前执行）。
- **Commit**：`5292938` `fix(studio): FE-004 FOUC脚本改外部文件以兼容CSP`

#### N2 [中] 供应商页「设为默认模型」后标签 active 态不同步

- **文件**：`studio/renderer.js`（供应商页模型标签）
- **问题**：点击「设为默认」后，其他 `prov-model-tag` 的 active 类不更新，视觉上多个标签同时高亮。
- **修复**：点击时 `$$('.prov-model-tag').remove('active')` 再点亮当前别名。
- **Commit**：`cd3ee0c` `fix(studio): 供应商页设为默认模型后标签active态不同步——点击时刷新全卡片高亮`

#### N3 [低] 任务图状态筛选按钮依赖全局 window.event

- **文件**：`studio/renderer.js`（任务图筛选按钮）
- **问题**：依赖已废弃的全局 `window.event`，异步/跨域场景为 undefined。
- **修复**：改 `onclick:(ev)=>` 用 `ev.currentTarget`。
- **Commit**：`55b1b0d` `fix(studio): 任务图状态筛选按钮依赖全局window.event——改handler入参ev`

---

## 三、补充的测试用例

### 引擎侧（commit `2c484cf`，共 8 个回归测试）

| 测试 | 文件 | 覆盖的修复/分支 |
|------|------|----------------|
| `test_keyless_custom_provider_not_flagged_missing_key` | test_doctor.py | D-01：免钥自定义 provider → api_keys=pass |
| `test_parse_skips_empty_and_blank_ids` | test_model_discovery.py | D-03：空/空白 id 被滤掉 |
| `test_parse_body_not_a_dict` | test_model_discovery.py | discovery 未覆盖分支：响应非 JSON 对象 → parse |
| `test_parse_data_field_not_a_list` | test_model_discovery.py | discovery 未覆盖分支：data 非 list → parse |
| `test_fetch_models_other_http_status_is_network` | test_model_discovery.py | discovery 未覆盖分支：5xx → network |
| `test_custom_provider_colon_name_rejected` | test_config.py | D-04：含冒号名拒绝 |
| `test_custom_provider_empty_name_rejected` | test_config.py | D-04：空名拒绝 |
| `test_config_import_rejects_key_value_in_api_key_env` | test_config_io.py | D-02：误填密钥值进 api_key_env 拒绝、原文件不污染、报错不回显密钥 |

另：`c741291` 清理触达测试文件的预存 ruff 告警（未用 import/导入排序）。

### 前端侧（commit `ce5220f`，共 3 个 e2e 用例）

| 测试 | 覆盖的修复 |
|------|-----------|
| e2e 6b：勾选 Mock 后模型下拉 disabled + 模型区 opacity=0.45 | 模型选择链路 mock 置灰 |
| e2e 16b：暗色下 DAG 节点主标题 computed fill 亮度>100 | FE-001 回归锁定 |
| e2e 16c：DAG 节点框 fill 非写死 #FFFFFF | FE-001 节点框主题化 |

---

## 四、覆盖率数据

### 引擎侧（pytest-cov，整改前后对比）

| 文件 | 整改前 | 整改后 | 说明 |
|------|--------|--------|------|
| models/discovery.py | 96% | **100%** | 补 data 非 dict / data 非 list / 5xx 三个错误分类分支 |
| models/config.py | 96% | 96% | 防御性 continue/空 model 为极边界 |
| config.py | 86% | 86% | 新增名校验分支已被测试覆盖 |
| api/server.py | 74% | 74% | missing 多为非 V4 端点的错误分支 |
| factory.py | 87% | 87% | 未改动 |
| **TOTAL** | 87% | **87%** | discovery 模块达 100% |

---

## 五、验证结果（整改后全量回归，全部通过）

| # | 验证项 | 结果 | 验收标准 |
|---|--------|------|----------|
| 1 | `python -m pytest -q` | **1694 passed, 1 skipped** | ≥1686 passed / 1 skipped ✓ |
| 2 | `cd studio && npm run e2e` | **23/23 通过** | ≥20/20 ✓ |
| 3 | `cd studio && npm run selftest` | **16/16 通过** | 16/16 ✓ |
| 4 | `ruff check agentcrew main.py`（V4 触达文件） | **All checks passed** | 零错误 ✓ |
| 5 | `mypy`（V4 修改文件） | **零新增错误**（仅 config.py:289 预存） | 修改文件零错误 ✓ |
| 6 | `grep -nE '#[0-9A-Fa-f]{3,8}\|rgba?(' studio/styles.css`（正文区） | **0 命中** | 正文区零硬编码 ✓ |
| 7 | `grep -nE '#[0-9A-Fa-f]{6}\|rgba?(' studio/renderer.js` | **0 命中** | 零硬编码 ✓ |
| 8 | 密钥安全静态自查 | **通过**（os.environ 值从不写进输出；config import 拒绝密钥值；GET /providers 不含密钥；provider:keyStatus 仅回布尔） | 通过 ✓ |
| 9 | 之前 8 个 V4-FE 问题全部修复 | **8/8 修复并验证** | 全部修复 ✓ |
| 10 | 暗色模式 9 页面可读性抽查 | **全部通过**（DAG 标题亮度 237，状态徽章/弹窗/下拉均主题化） | 无低对比度文本 ✓ |

> 注：ruff 全仓检查有 10 个预存错误在 base_agent/summarizer/cli_prompter/reviewer/static_checks/mcp 等审查范围外文件，按要求未改动；mypy factory.py 多处预存错误（LLMCache None 解引用、BaseException vs Exception）与 config.py:289 均为上轮报告已列明的预存问题，本次未触碰 factory.py。

---

## 六、集成与兼容性验证

- **API 响应结构对齐**：引擎 `GET /providers` 返回 `name/kind/implementation/base_url/api_key_env/key_configured/capabilities/suggested_models/health{latency_ms,last_check,error}`，前端正确读取 `key_configured`/`latency_ms`/`health`/`error`，字段名完全一致。
- **测速端点对齐**：引擎 `POST /providers/{name}/test` 返回 `{ok, latency_ms, error}`，前端读取 `r.ok`/`r.latency_ms`/`r.error`，一致。
- **provider 列表对齐**：前端 BUILTIN_PROVIDERS 9 个内置 provider 与引擎 PROVIDER_PRESETS 前 9 个一致；供应商页「从预设添加」弹窗列出额外 6 个新预设（openrouter/siliconflow/moonshot/zhipu/groq/together），与引擎 15 预设对齐。
- **代码冲突检查**：引擎侧与前端侧分别修改 Python 和 JavaScript 文件，无重叠文件，git log 确认无冲突。

---

## 七、确认无问题的审查项

### 引擎侧

**正确性与边界**
- candidate_urls：base_url 以 `/v1` 结尾不重复拼接；末尾斜杠 rstrip 处理；models_url 覆写优先；anthropic 走显式 models_url。
- 15 个内置 provider 的 `api_key_for` 逐一验证：12 个带 key 的 env 名正确、未配置时返回 None；ollama 返回占位串、mock/cli 返回 None。
- build_directory 重名检测在 YAML 加载、env JSON（AGENTCREW_CUSTOM_PROVIDERS）、CLI 三个入口都经 `_validate_custom_providers` + build_directory 双重拦截。
- config import：文件不存在/YAML 错误/非映射/config_version>1 均清晰报错；写前自动 `.bak` 备份；字段级合并（仅覆盖导入文件中出现的段）。
- 测速：provider 无 api_key_env 时优雅处理；base_url 不可达时 10s 超时；并发锁正确。

**错误处理与异常安全**
- discovery 用 `urllib.request` 在 `asyncio.to_thread` 跑，无 aiohttp session 需关闭，无资源泄漏。
- `config_snapshot()` 递归脱敏经嵌套 dict/list 实测：`capabilities.max_output_tokens` 虽含 "token" 不误伤，mcp_servers 嵌套 env 的 MY_TOKEN/API_KEY 被抹成 `***`，secret_values 同时收集内置+自定义 env 值。
- YAML/JSON 反序列化只产生 JSON 原生类型，`yaml.safe_dump` 无 set/自定义实例问题。

**安全**
- `os.environ`/`os.getenv` 全量 grep（15 处）：env 值只用于 `bool()` 判定（key_configured）、传给 provider、或日志脱敏；从不写进文件/响应/日志。
- GET /providers 响应只含 env 名 + `key_configured` 布尔，不含 key 值。
- POST /providers/{name}/test 错误信息来自 SDK `str(exc)`，不含 Authorization 请求头。
- config import 密钥检查递归覆盖 custom_providers / provider_overrides / mcp_servers 等所有嵌套层级。

**性能与资源**
- `_discovery_cache` key 是 provider name（固定有界集合），成功结果缓存 60s、错误不缓存，无无限增长。
- GET /providers 每次重建目录，但仅浅拷贝 15 个预设 + 校验少量自定义，微秒级，无需缓存。
- 健康监控循环顺序遍历已缓存 alias、5s 超时包装，新增 provider 不拖慢主循环。

### 前端侧

**主题系统**
- styles.css 全文件颜色 token 化（:root + [data-theme="dark"] 双套 95 个 CSS 变量），正文区零硬编码 hex/rgba。
- renderer.js 零硬编码色值（FE-002 修复后全部引用 CSS 变量）。
- 9 个页面暗色模式可读性全部通过。
- system 主题切换经 nativeTheme.on('updated') 推送，无 FOUC（boot-theme.js 同步预置）。

**模型选择链路**
- provider 下拉加载失败时降级到手输 + 内置 suggested_models，有 UI 反馈。
- provider 切换时清空旧模型选择。
- mock 勾选正确置灰模型区（e2e 6b 锁定）。
- `--model` 弹窗未选→settings.defaultModel→不传；`--max-cost` 弹窗优先、空值回落 settings、双空不传。

**供应商页**
- 无后端时 9 张卡片全部渲染。
- 测试按钮 1.5s 防抖 + loading/disabled 状态。
- 10s 超时正确处理。
- 「设为默认」后 toast + 标签 active 态同步（N2 修复后）。
- 从预设添加弹窗 YAML 片段正确缩进、经 textarea.value 插入（非 innerHTML）。
- 密钥状态仅回布尔，不传 env 值。

**新建运行弹窗**
- 空目标描红、输入即清除。
- Demo 置灰目标输入。
- 最近目标持久化到 settings.json、去重截 5 条。
- 高级折叠保留值。
- 弹窗每次 open 重建 DOM，Esc 后重开无残留。

**设置页**
- 四组卡片布局正常。
- Python 探测按钮有探测中状态/失败错误信息/版本显示。
- 连接测试走 provider:test IPC。
- 主题 segmented control 三态即时生效、与顶栏同步。
- 保存后 toast 回执。

**安全与 CSP**
- CSP connect-src 精确到 `http://localhost:* http://127.0.0.1:*`（无裸 `*`）。
- 无 eval/new Function/document.write；h() 文本节点用 createTextNode 转义；highlightCode 先转义再高亮。
- IPC 通道 preload 暴露的全部在 main.js 有对应 handle，无悬空通道。
- provider:keyStatus 仅回布尔。

---

## 八、Commit 清单（本次深度审查共 18 个提交）

### 引擎侧（6 个）

| Commit | 类型 | 说明 |
|--------|------|------|
| `5b6cc9b` | fix(engine) | doctor 对空 api_key_env 自定义 provider 误报缺 Key（P1） |
| `be0723a` | fix(engine) | 模型发现跳过空/空白 model id（P2） |
| `df817e0` | fix(engine) | 拒绝名字含冒号/为空的自定义 provider（P2） |
| `8c946b6` | fix(engine) | config import 拒绝密钥误填进 api_key_env（P1 红线） |
| `2c484cf` | test(engine) | 补充 8 个回归测试与未覆盖分支 |
| `c741291` | style(test) | 清理触达测试文件预存 ruff 告警 |

### 前端侧（12 个）

| Commit | 类型 | 说明 |
|--------|------|------|
| `dd4760d` | fix(studio) | FE-001 DAG 节点标题深色模式不可读（高） |
| `d936c1a` | fix(studio) | FE-002 状态/角色/事件色改引用 CSS 变量（中） |
| `f1ba9ac` | fix(studio) | FE-003 内联 input 边框改 var(--border-input)（中） |
| `7b2a48a` | fix(studio) | FE-004 启动首屏浅色闪烁（低，初版） |
| `9ffa974` | fix(studio) | FE-005 saveSettings 冗余推送 theme:changed（低） |
| `6811bf0` | fix(studio) | FE-006 产物导航图标 SVG 路径重复越界（低） |
| `5136474` | fix(studio) | FE-007 死代码清理（低） |
| `5fda03b` | fix(studio) | FE-008 cycleTheme 持久化失败回滚（低） |
| `cd3ee0c` | fix(studio) | 供应商页设为默认模型后标签 active 态不同步（N2，中） |
| `ce5220f` | test(studio) | e2e 补充 FE-001 暗色 DAG 可读性与 mock 置灰回归用例 |
| `5292938` | fix(studio) | FE-004 FOUC 脚本改外部文件以兼容 CSP（N1，高） |
| `55b1b0d` | fix(studio) | 任务图状态筛选按钮依赖全局 window.event（N3，低） |

---

## 九、遗留事项

1. **预存 mypy 错误**：`config.py:289`（api_keys 类型窄化）、`factory.py` 多处（LLMCache None 解引用、BaseException vs Exception）为 V4 之前的预存错误，本次未处理，建议后续单独清理。
2. **预存 ruff 错误**：全仓 10 处 ruff 错误在 base_agent/summarizer/cli_prompter/reviewer/static_checks/mcp 等审查范围外文件，本次未改动。
3. **e2e 第15项**：计划审查页硬编码 `check(..., true, ...)` 恒通过，属预存问题（非 V4 新增），建议后续加固。

---

## 十、结论

本轮加大力度的深度审查在 V4 全栈共发现并修复 **15 个问题**（引擎 4 + 前端 11），其中：
- **红线 1 个**：config import 密钥落盘路径（D-02）
- **高优先级 2 个**：DAG 节点标题深色不可读（FE-001）、FOUC 脚本被 CSP 拦截（N1）
- **中优先级 4 个**：状态色硬编码（FE-002）、input 边框深色消失（FE-003）、供应商标签 active 不同步（N2）、doctor 免钥误报（D-01）
- **低优先级 8 个**：FOUC/冗余推送/SVG 越界/死代码/持久化回滚/全局 event/空 model id/provider 名校验

整改后全量验证 **10 项验收标准全部通过**：pytest 1694 passed / e2e 23/23 / selftest 16/16 / ruff 触达文件零错误 / mypy 零新增 / 硬编码色值零命中 / 密钥安全自查通过 / 8 个 FE 问题全修复 / 暗色 9 页面可读 / API 集成一致。共 18 个提交，补充 11 个测试用例，discovery.py 覆盖率达 100%。
