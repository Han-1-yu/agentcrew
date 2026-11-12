# 能力补齐：联网搜索 / 沙箱执行 / 执行验证（C1 · C2 · C3）

> 立项依据：`完善/AgentCrew落地完善路线-2026-11-12.md` §2（C1/C2/C3）与 §5 第一批。
> 本文是**落地说明**：怎么开、边界在哪、怎么证明它真的在工作。
> 相关背景（为什么做这三件事）见路线文档，本文只写「现在有什么、怎么用、坑在哪」。

---

## 0. 一句话

补上了三根支柱里最缺的两根的一半：

- **能查**：`web_search`（C1）——Agent 可以主动检索，不再只会抓已知 URL；
- **敢跑**：`ExecutionBackend`（C2）——代码可以进容器跑（禁网、非 root、资源上限）；
- **跑得出结论**：`run_tests` + G1 执行校验（C3）——生成的代码**被执行验证**，
  失败进既有返工通道，而不是靠评审模型「看代码打分」。

三者共用一条**默认关闭**红线：不配就没有这些能力，配了才出现，且都能离线测。

---

## 1. C2 · 可插拔执行后端

### 1.1 两个后端

| 后端 | 边界 | 适用 |
| --- | --- | --- |
| `local`（默认） | `-I` 隔离、裁剪环境变量（只留 PATH/SYSTEMROOT/TEMP/TMP/PYTHONIOENCODING）、`cwd` 锁工作区、超时即杀 | 本机开发、离线测试。**没有**网络 / CPU / 内存 / 系统调用隔离 |
| `docker` | `--network none`、`--user 65534:65534`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--read-only` + `/tmp` tmpfs、`--memory` / `--cpus` / `--pids-limit`、只挂 `<run_dir>:/workspace:rw` | 生产环境跑模型写的代码 |

**选了 `docker` 却没有 docker 时，`run` 直接失败**（`SandboxError`），不会静默
退回 `local`——「以为自己有沙箱」比「没有沙箱」更危险。

### 1.2 配置（四通道）

| 通道 | 写法 |
| --- | --- |
| YAML | `sandbox_backend: docker` · `sandbox_image` · `sandbox_memory_mb` · `sandbox_cpus` · `sandbox_allow_network` |
| 环境变量 | `AGENTCREW_SANDBOX_BACKEND` / `_IMAGE` / `_MEMORY_MB` / `_CPUS` / `_ALLOW_NETWORK` |
| 导出/导入 | `main.py config export` → `config import`（新字段自动往返） |
| API options | **刻意不开放** `sandbox_backend`：它决定代码在哪跑，允许按 run 覆盖等于允许任意 `runs:write` 把隔离降级回 `local`（同 `webhook_url` 的理由） |

### 1.3 用法

```bash
# 生产：容器内跑代码（需要宿主机有 docker；镜像里要有 pytest 才能跑 run_tests）
AGENTCREW_TOOLS_ENABLED=true \
AGENTCREW_CODE_EXEC_ENABLED=true \
AGENTCREW_SANDBOX_BACKEND=docker \
AGENTCREW_SANDBOX_IMAGE=python:3.12-slim \
python main.py run --demo

# 本机：不开代码执行时这些工具连清单都不出现
python main.py tools
```

### 1.4 已验证 / 未验证

- 已验证（`tests/test_sandbox_backend.py`）：local 行为逐字不变、docker 参数逐项
  存在（禁网/非 root/cap 全丢/资源上限/只挂工作区）、缺 docker 时构造期报错、
  运行期 docker 消失时报错并留痕。
- **未实测**：本机没有 Docker，容器真跑一遍需要目标机执行
  `docker run` 冒烟（见 §5 待办）。这一点在 `PROJECT-STATE.md` 里明确记为未验证，
  不冒充「已跑通」。

---

## 2. C1 · 联网搜索 `web_search`

### 2.1 提供方

| 名字 | 密钥 | 说明 |
| --- | --- | --- |
| `tavily` | `TAVILY_API_KEY` | 面向 LLM 的搜索结果，摘要质量好 |
| `brave` | `BRAVE_API_KEY` | Brave Search API |
| `duckduckgo` | 免密钥 | 非官方 HTML 端点，可能限流；适合本机 smoke，不建议生产主力 |
| `mock` | — | 离线夹具，只给测试与评测用 |

密钥只从环境变量读（`AGENTCREW_SEARCH_API_KEY` 优先于提供方专属变量），
**绝不进配置、快照、日志**。

### 2.2 边界

- 端点写死为公网 https，**不跟随重定向**（复用 `web_fetch` 的 no-redirect opener）；
- 返回结果按 URL 去重，且逐个过 `validate_fetch_url`——`file://`、内网 IP、
  带内嵌凭据的 URL 一律丢弃，模型看不到；
- 输出长度上限 8000 字符（可调），超出截断并显式说明；
- `search_provider` 为空时**不注册** `web_search`：不承诺一个跑不了的能力。

### 2.3 用法

```bash
# 生产（Tavily）
AGENTCREW_TOOLS_ENABLED=true \
AGENTCREW_SEARCH_PROVIDER=tavily \
TAVILY_API_KEY=tvly-*** \
python main.py run --demo

# 本机 smoke（免密钥）
AGENTCREW_TOOLS_ENABLED=true AGENTCREW_SEARCH_PROVIDER=duckduckgo \
python main.py tool web_search --args '{"query": "agent framework", "max_results": 3}'
```

---

## 3. C3 · 执行-测试-修正闭环

### 3.1 两个入口

1. **Agent 主动跑**：`run_tests` 内置工具（`code_exec_enabled` 打开时出现）。
   参数只有 `path` 与 `timeout`——**不接受任意 pytest 参数**，否则
   `--rootdir=/etc` 一类写法会绕过路径边界。输出保留尾部 4000 字符（报错在尾部），
   并显式标注省略了多少字符。不写 `.pytest_cache`（run 目录要长期保留、要 `diff`）。
2. **门禁兜底跑**：G1 执行校验档（`gate_config.exec_verify: true`，默认关）。
   静态校验放行后，把该任务产物铺进 `<run_dir>/verify/<task_id>/` 跑 pytest：
   - 没有 `test_*.py` / `*_test.py` → 记 `g1_exec_check` `status=skipped`（不假装通过）；
   - 失败 → issue `执行校验失败（pytest exit_code=N）：…`，报错原文进 `fix_hint`，
     状态走**既有** `REWORK` 通道（不新增第二条返工路径）；
   - 超时 / 后端不可用 → 同样按失败处理并说明原因（不静默放行）。
   - `mock` 通道显式跳过并记 `g1_exec_check_skipped`：夹具里的测试常 import
     别的任务的模块，只铺单个任务产物必然 import 失败——那不是产物的问题。

### 3.2 配置

```yaml
gate_config:
  exec_verify: true              # 默认 false
  exec_verify_timeout_seconds: 120
```

`exec_verify` 经 `gate_config` 通道下发（YAML / API options 都可），随
`config_snapshot` 落盘。

### 3.3 已知限制

- `docker` 后端跑 `run_tests` 需要镜像里有 pytest（默认 `python:3.12-slim` 没有）；
  缺失时输出会明确提示「把 pytest 打进 sandbox_image」，不会静默通过。
- 执行校验只覆盖「任务自己交付了测试」的产物；没有测试就跳过——它不生成测试。

---

## 4. C11 · 评测证据（配套）

新增两套数据集与一个夜间脚本，**mock 与真实双轨**：

| 文件 | 通道 | 内容 |
| --- | --- | --- |
| `examples/evals/smoke.jsonl` / `revision.jsonl` | mock | 原有回归 |
| `examples/evals/capabilities.jsonl` | mock | 4 条：新工具是否进注册表、默认是否仍只有 5 个工具、exec_verify 在 mock 下是否显式跳过 |
| `examples/evals/capability-live.jsonl` | 真实模型 | 23 条小目标：代码 8 / 文档 6 / 数据 6 / 驳回 3 |

新增断言：

- `event_field`：读事件载荷（如 `tool_registry_ready.tools` 是否含 `run_tests`），
  比「事件出现了 1 次」实在；
- `artifact_any_contains` / `artifact_any_matches`：在**任意**产物里找证据，
  真实模型自己决定文件名时也能断言。

跑法：

```bash
# 离线（CI / 本地，零成本）
python main.py eval examples/evals/capabilities.jsonl --repeat 2

# 真实模型（有成本，必须带硬上限）
python main.py eval examples/evals/capability-live.jsonl --live --max-cost 1.50

# 夜间：自动落盘到 bench/eval-<数据集>-<通道>-<日期>.json
python scripts/nightly_eval.py --max-cost 1.50
```

`--max-cost` 是硬上限（预算判定用 `total_cost_usd > max_cost_usd`），`nan` / `inf`
在 CLI 入口就被拒——否则与 NaN 比较恒为 False，等于没设上限。

---

## 5. 待办（本机无法验证的部分）

| 项 | 为什么没做 | 怎么补 |
| --- | --- | --- |
| Docker 后端真跑一遍 | 本机没有 docker 可执行文件 | 目标机 `AGENTCREW_SANDBOX_BACKEND=docker AGENTCREW_CODE_EXEC_ENABLED=true` 跑一次 `run_tests`，并把逃逸用例（出网 / 读宿主 / 资源耗尽）结果补进 `docs/THREAT_MODEL.md` |
| 真实模型 23 条评测报告 | 需要密钥与预算 | 有密钥的机器上 `python scripts/nightly_eval.py --max-cost 1.50`，把 `bench/eval-*-live-*.json` 结论回填 `PROJECT-STATE.md` |
| `run_tests` 在容器内跑通 | 依赖镜像内置 pytest | 自建镜像 `FROM python:3.12-slim && pip install pytest`，设为 `sandbox_image` |
