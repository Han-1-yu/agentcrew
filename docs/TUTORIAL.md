# AgentCrew 使用教程

> 目标读者：第一次用 AgentCrew 的人。按顺序做，能在一台机器上跑通「离线演示 → 真实模型
> → 自己的项目 → 人工卡点 → 工具链 → 对外服务」。
> 更细的设计说明看 `README.md`，部署与排障看 `docs/OPERATIONS.md`，能力边界看
> `docs/THREAT_MODEL.md`。

---

## 0. 你需要什么

| 项 | 要求 |
| --- | --- |
| Python | 3.10+（推荐 3.12） |
| 模型 Key | 跑真实模型时需要；只跑离线演示不需要 |
| Node.js | 只在用桌面端 Studio 时需要（20+） |
| 磁盘 | 每个 run 占几十 MB 到几百 MB，取决于产物 |

AgentCrew 的分工是：**规划**由项目经理用一次受治理的 LLM 调用拆任务 DAG，**执行**由
确定性调度器派发，模型只负责「想」和「写」，调度、状态流转、依赖解析、契约校验全是
可测试的代码。

---

## 1. 十分钟跑通（不需要 Key）

### 1.1 安装

```bash
cd E:/agentcrew

# 用已有虚拟环境（推荐，仓库自带 .venv）；没有就建一个
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

### 1.2 自检

```bash
python main.py doctor
```

它会检查 Python 版本、依赖、API Key、`default_model` 是否可解析、输出目录是否可写，
并对已配置的 provider 做一次连通性探测。**任何一项失败退出码为 3**，先修好再往下走。

### 1.3 用预设样例离线跑一次

```bash
python main.py run --sample linear --no-live --auto
```

`--sample` 会自动开 mock（无需 Key）。预期看到类似：

```
任务      5 个   成功 5 · 失败 0 · 阻塞 0 · 跳过 0
评审通过率 100%（G1 拦截 0 / G2 驳回 0 / 人工驳回 0）
产物      E:\agentcrew\output\2026....\artifacts
```

四个内置样例：

| 样例 | 形状 | 任务数 |
| --- | --- | --- |
| `todo` | 混合并发（Web 应用） | 9 |
| `linear` | 严格串行（日志工具） | 5 |
| `revision` | 首轮复核不通过（演示返工） | 5 |
| `hierarchy` | 两层并行 + 汇合 | 7 |

```bash
python main.py samples     # 列出样例与各自的目标
```

### 1.4 看懂产物

```bash
python main.py status <run_id>
```

每个 run 在 `output/<run_id>/` 下：

```
run.json          运行记录（状态、目标、统计）
tasks.json        任务表（含每次评审记录）
ledger.jsonl      全事件审计（run/task/llm_call/gate/tool_call…）
artifacts/        交付物 + MANIFEST.json（路径/type/size/sha256）
workspace/        工具链工作区（CLI 执行体、generate_image 落盘处）
context/ review/ diagnostics/
```

`MANIFEST.json` 是产物的权威清单：拿 `sha256` 可以核对文件有没有被改过。

---

## 2. 接真实模型

### 2.1 配 Key

```bash
cp .env.example .env    # PowerShell: Copy-Item .env.example .env
```

编辑 `.env`，至少填一个 provider：

```bash
DEEPSEEK_API_KEY=sk-...
# 或 OPENAI_API_KEY=sk-...
# 或 ANTHROPIC_API_KEY=sk-ant-...
AGENTCREW_DEFAULT_MODEL=deepseek:deepseek-chat
```

**密钥只走环境变量**，不要写进 `agentcrew.yaml`——配置文件会被导出、进快照、进日志。

### 2.2 验通

```bash
python main.py doctor
# ✓ provider:deepseek:deepseek-chat: healthy（700ms）
```

### 2.3 跑内置目标

```bash
python main.py run --demo                  # 内置 Todo Web 目标
python main.py run --goal "你的项目目标"
```

要点：

- `--demo` 与 `--goal` 二选一，都不给或都给会以退出码 3 报错。
- 默认 `gate=both`：规划后、封版前各一个人工确认点。**非交互环境（管道/CI）会自动降级
  为 `gate=none` 并在 stderr 说明**，不会偷偷放行；想全程无人值守加 `--auto`。
- 想省钱先设上限：`--max-cost 0.5 --max-llm-calls 60`。

---

## 3. 跑自己的项目

### 3.1 目标怎么写

目标写得越像「验收单」，拆解越稳。建议包含：

1. **交付什么**：具体文件或可运行的东西；
2. **验收标准**：怎样算完成（能跑、包含哪些内容）；
3. **边界**：不做什么、不要哪些技术；
4. **规模暗示**：几个模块、要不要测试和文档。

例：

```bash
python main.py run --goal "做一个命令行记账工具 ledger：
支持 add/list/sum 三个子命令，数据存本地 JSON；
附 pytest 用例（覆盖正常与异常输入）和 README（安装/用法/示例）。
不写 Web 界面，不引第三方依赖。" --max-cost 0.5
```

### 3.2 三个常用开关

```bash
python main.py run --goal "..." --plan-only     # 只出计划不执行（Ask 模式）
python main.py run --goal "..." --auto          # 全自动，跳过人工卡点
python main.py run --goal "..." --gate plan     # 只在规划后卡一次
```

### 3.3 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 全部任务成功 |
| 1 | 有 FAILED / BLOCKED / SKIPPED |
| 2 | 拆解连续不合格（重试用尽） |
| 3 | 配置/参数错误（缺 Key、resume 配置不一致等） |
| 4 | 停滞（无可运行任务但仍有未终结任务） |
| 130 | Ctrl-C 中断或 cancel |

---

## 4. 人工卡点与断点调试

### 4.1 跨进程审批（推荐用于服务器/无人值守）

```bash
# 1) 起一个跑到卡点就挂起的 run
python main.py run --goal "..." --gate plan --approval-mode deferred --no-live

# 2) 看待审批单
python main.py approvals <run_id>

# 3) 在任意进程里裁决
python main.py approve <run_id> <approval_id> --decision approve --by alice

# 4) 回到原卡点继续（resume 要带上与创建时一致的参数，配置不一致加 --force）
python main.py resume <run_id> --gate plan --approval-mode deferred --force --no-live
```

`deferred` 的 run 会以 `awaiting_approval` 非终态**正常退出（退出码 0）**，不是崩溃；
`clean` / `delete` 会保护这类 run。

### 4.2 单步调试

```bash
python main.py run --goal "..." --debug-step --approval-mode deferred --no-live
python main.py approve <run_id> <approval_id> --decision step   # 放行一个任务
python main.py approve <run_id> <approval_id> --decision run    # 解除断点
python main.py approve <run_id> <approval_id> --decision abort  # 中止
```

### 4.3 中断后续跑

`Ctrl-C` 会把 run 落成 `INTERRUPTED`。`resume` 的语义：

- `RUNNING` 的任务回到 `READY` 重跑（产物可能写了一半）；
- `REWORK` 保持原状，调度器直接重派；
- `REVIEW` 保持原状，只重跑门禁；
- `attempts` / `rework_count` **不重置**。

### 4.4 从某一步重做（`rewind`）

`resume` 只能「接着跑」；想改一个已完成任务的交付，用它：

```bash
python main.py rewind <run_id> --task T-002 --dry-run   # 先看影响集
python main.py rewind <run_id> --task T-002 \
  --instruction "输出改成 Markdown 表格，加一列 p95"      # 真回滚
python main.py resume <run_id>                          # 从 T-002 重跑
```

- 影响集 = 该任务 + 依赖它的全部下游 + 运行时 `parent_id` 子任务 + `T-FINAL`；
  上游已成功的任务与产物**不动**，不用重花上游的 token。
- 旧产物先复制到 `history/rewind-NNN/`（`rewind.json` 里有影响集、旧任务快照与
  逐文件 sha256），再从 `artifacts/` 移除，因此回滚可追、可取证。
- run 状态置回 `INTERRUPTED`，并记一条 `run_rewound` 事件；**只接受已停下来的 run**
  （正在跑或挂审批的先 `cancel` / `approve`）。
- `--instruction` 会写进目标任务描述与验收标准，等于给这次重做加一条可被 G2 评审
  的要求。

---

## 5. 打开工具链

工具**默认关闭**，两步开启：

```yaml
# agentcrew.yaml
tools_enabled: true          # 读文件/列目录/检索/写文件/网页抓取
code_exec_enabled: true      # 额外注册 run_python / run_tests（有执行风险，按需开）
sandbox_backend: local       # 想真隔离改成 docker（禁网/非 root/资源上限）
```

按角色收权：

```yaml
tool_permissions:
  backend_dev: [read_file, grep, run_python, run_tests]
  technical_writer: [read_file, grep]
```

两个按需才出现的工具：

```yaml
# 联网搜索（不配就没有这个工具，模型看不见）
search_provider: tavily        # tavily / brave / duckduckgo
# 密钥只从环境变量读：TAVILY_API_KEY / BRAVE_API_KEY

# 图像生成（不配就没有 generate_image）
image_provider: ark            # ark / openai / mock
# 密钥：ARK_API_KEY / OPENAI_API_KEY
```

单工具试跑（沙箱目录隔离）：

```bash
python main.py tools
python main.py tool read_file --args '{"path": "README.md"}'
```

> 输入来自外部不可信文档时设 `untrusted_inputs: true`：只下发只读工具，写工具一律不给。

---

## 6. 异构执行体：把任务指派给不同「人」

默认每个角色绑一个模型。想让「豆包写文案、Claude Code 写代码、Codex 统筹」，配一个
执行体池，PM 拆解时会看到能力菜单并按任务指派。

```yaml
agents:
  profiles:
    planner:
      title: 统筹规划
      description: 需求拆解、架构设计、方案复核，强推理
      capabilities: [plan, design, review]
      model: deepseek:deepseek-reasoner

    coder:
      title: 代码开发
      description: 实现、重构、单元测试，工程规范优先
      capabilities: [code, refactor, test]
      model: claude-code:default     # R60：真 agentic CLI，在工作区自主多步
      tools: [read_file, grep, run_python, run_tests]

    image:
      title: 图像生成
      description: 配图、海报、插画
      capabilities: [image, illustration]
      model: deepseek:deepseek-chat  # 文本推理档，产图走工具
      tools: [generate_image]

roles:
  architect:   {agents: [{ref: planner, duty: primary}]}
  backend_dev: {agents: [{ref: coder, duty: primary}]}
```

派发优先级是确定的：**运行时人工覆盖 > 规划期指派 > 角色默认绑定**。指派到不存在或已
失效的执行体不会拖垮 run，会记一条 `task_agent_assignment_dropped` 并回退默认绑定。

关于 `claude-code` / `codex`：

- 需要本机已安装对应 CLI 并完成登录（平台不持有它们的密钥）；
- 以 run 的 `workspace/` 为工作目录自主多步执行；
- 安全默认写在预设命令里：Claude 只自动接受文件编辑，Codex 沙箱限工作区写。

完整示例见 `examples/agentcrew.heterogeneous.example.yaml`。

### 验证图像链路（离线字节，不花钱）

```bash
AGENTCREW_TOOLS_ENABLED=true AGENTCREW_IMAGE_PROVIDER=mock \
  python main.py run --flow examples/flows/image-e2e.yaml --auto --no-live \
  --default-model deepseek:deepseek-chat
```

跑完检查 `artifacts/MANIFEST.json` 里的 `kind=image` 与 `sha256` 是否和磁盘文件一致。
真实出图把 `image_provider` 换成 `ark` / `openai` 并配好 Key。

---

## 7. 可视化编排（跳过 PM 直接按图执行）

```bash
python main.py flow sample > my-flow.yaml     # 拿一份示例
python main.py flow validate my-flow.yaml     # 校验：id 唯一、依赖存在、无环
python main.py run --flow my-flow.yaml --auto --no-live
```

流程 YAML 与 PM 的拆解结果同一套 schema（`TaskDraft`）：

```yaml
version: 1
name: my-flow
description: 一句话说明
tasks:
  - id: T1
    title: 写 PRD
    description: 输出需求文档
    role: product_manager
    acceptance_criteria: [包含目标用户, 包含验收标准]
    deliverables:
      - path: docs/PRD.md
        kind: markdown
        description: 产品需求文档
    priority: 9
```

也可以用 Studio 的画布拖拽生成，再导出 YAML。

---

## 8. 对外提供服务

### 8.1 HTTP API + Web UI

```bash
export AGENTCREW_API_KEYS="dev-key-123"    # 认证默认开启，fail-closed
python main.py serve --port 8765
```

- 只绑回环时允许无 Key 启动（本地开发，带显式警告）；绑非回环必须配 Key，否则拒绝启动。
- 请求带 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。
- 常用端点：`POST /runs`（创建）、`GET /runs/{id}/status`、`GET /runs/{id}/events`（SSE）、
  `GET /runs/{id}/ledger`、`GET /runs/{id}/stats`、`POST /runs/{id}/cancel`、
  `POST /runs/{id}/resume`、`GET /metrics`。

### 8.2 一键启动桌面端（Windows）

不想开两个终端，直接双击仓库根的 **`start-agentcrew.cmd`**：

1. 起后端 `main.py serve`（只绑 `127.0.0.1:8765`），等 `/health` 通过；
2. 首次运行把 Studio 的 `apiBaseUrl` 预填成本机地址（只填空值，不动其他设置）；
3. 打开 Studio 窗口；**关窗即回收后端**（Ctrl-C 同样回收）；
4. 后端日志在 `logs/desktop-serve.log`，起不来时脚本会打印最后几行。

```bash
start-agentcrew.cmd                 # 双击等价
start-agentcrew.cmd --port 8766     # 换端口
start-agentcrew.cmd --api-key dev   # 开启认证（需在 Studio 设置里填同一个 Key）
start-agentcrew.cmd --no-studio     # 只起后端
start-agentcrew.cmd --smoke         # 起后端→验 /health→关闭（自检用）
```

安全边界：默认只在回环地址监听并**关闭认证**（本机桌面场景）；要给别的机器访问，
请用 `python main.py serve --host 0.0.0.0 --api-key ...` 并按 `docs/OPERATIONS.md` 配反代。

想在桌面放个快捷方式：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
```

macOS / Linux 直接跑编排脚本：`python scripts/desktop.py`。

### 8.3 桌面端 Studio

```bash
cd studio
npm install
npm start
```

连的是本机 `serve`。页面包含运行列表、看板、事件流、任务详情、产物、图表、供应商、设置。
启动器已把后端地址预填好；如果没有，在 Studio 的「设置 → 后端 API 地址」填
`http://127.0.0.1:8765`，开了认证的话再把 Key 填进「后端 API Key」。

### 8.4 在别的程序里调用（Python SDK）

```bash
pip install -e ./sdk
```

```python
from agentcrew_sdk import AgentCrewClient

with AgentCrewClient("http://127.0.0.1:8765", api_key="dev-key-123") as c:
    run_id = c.create_run("做一个 Todo 应用", options={"mock": True})["run_id"]
    final = c.wait_for_run(run_id, timeout=600)
    print(final["status"], c.get_stats(run_id))
```

零依赖（只用 `urllib`），支持 SSE、ledger 分页、按状态码分型的异常。详见 `sdk/README.md`。

### 8.5 作为 MCP server 给 Claude Desktop / Cursor 用

```bash
python main.py mcp-serve --api-url http://127.0.0.1:8765 --api-key dev-key-123
```

客户端配置（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "agentcrew": {
      "command": "python",
      "args": ["E:/agentcrew/main.py", "mcp-serve",
               "--api-url", "http://127.0.0.1:8765"],
      "env": {"AGENTCREW_MCP_API_KEY": "dev-key-123"}
    }
  }
}
```

暴露 5 个工具（创建 run、查进度、看任务、裁决审批等），全部复用 HTTP API 的 RBAC 与审计。

### 8.6 出站 Webhook

```yaml
# agentcrew.yaml
webhook_url: https://hooks.example.com/agentcrew
webhook_secret: ""             # 配了就用 HMAC-SHA256 签名（含时间戳，防重放）
webhook_events: [run_finished, run_stalled]
webhook_timeout_seconds: 10
webhook_max_retries: 3
```

语义是 **at-least-once**：接收方必须用 `X-AgentCrew-Delivery` 做幂等。未投出的通知会落盘，
下次启用 Webhook 的 run 启动时补投。

---

## 9. 常见问题

| 症状 | 处理 |
| --- | --- |
| `doctor` 报缺 Key | 检查 `.env` 是否在仓库根、变量名是否正确；`AGENTCREW_CONFIG` 是否指到了别的配置 |
| 402 Insufficient Balance | 供应商余额不足，不是代码问题；换 provider 或充值 |
| 任务反复被 G1 驳回 | `status <run_id>` 看 issues；ledger 里 `gate_result` 带 `fix_hints`，通常是对不上契约（路径/符号/体积） |
| `resume` 退出码 3 | 配置快照与创建时不一致；确认后加 `--force` |
| 本机 Python 偶发崩溃 | 已知环境问题；用 `python scripts/test.py` 的崩溃隔离跑器，或直接交给 CI |
| Studio 连不上 | 先确认 `serve` 在跑；端口/Key 是否一致 |
| 产物是 mock 的 | 用了 `--sample` 或 `--mock`；真实模型请用 `--goal` / `--demo` |

---

## 10. 上线前

```bash
python main.py doctor                     # 本机自检
python scripts/deploy_check.py            # 目标机 10 项判据（PASS/FAIL/SKIP）
python scripts/test.py --batch 2          # 全量回归（崩溃隔离）
```

必看：`docs/OPERATIONS.md`（部署形态、密钥边界、备份、监控、扩容、升级）、
`docs/DEPLOY-CHECK-2026-11-13.md`（逐项判据）、`docs/THREAT_MODEL.md`（能防什么、不能防什么）。
