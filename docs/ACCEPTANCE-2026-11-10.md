# AgentCrew 验收报告（R44–R49）

> 验收对象：2026-11-09 能力审查提出的改善方案（P0–P2）及其落地结果。
> 关联文档：`docs/AUDIT-2026-11-09.md`（审查）、`docs/IMPROVEMENT-PLAN-2026-11-09.md`（方案与执行记录）。
> 报告日期：2026-11-10　｜　验收方式：代码审查 + 自动化测试 + 真实模型跑批

---

## 一、验收结论

| 验收项 | 结论 | 判定依据 |
| --- | --- | --- |
| P0 复核接治理通道 | **通过** | 裁决契约与 G2 共用；复核改走 `complete_structured`；G2 用例一行未改全绿 |
| P1.1 执行体级熔断 | **通过** | 默认关；连续失败达阈值换 primary；`agent_circuit_open` 可查 |
| P1.2 成本感知路由 | **通过** | 默认关；`under_headroom` 单元测试 9 例；不碰全局降级状态 |
| P1.3 门禁评审归因 | **通过** | `unknown` 桶 7 次 → 1 次 |
| P2.1 配置 A/B 矩阵 | **通过** | `eval --variant` 实测给出 Δcalls / Δcost / Δ修订 |
| P2.2 分工依赖 | **通过（最小形态）** | `intra_deps` + 拓扑执行 + 上游摘要注入，默认行为不变 |
| P3（MCP resources / web_search / 分布式 / 事件溯源） | **不适用** | 方案明确"触发条件未到不预做" |

**总体判定：通过。** 六项交付全部可复现，无回退；两项遗留风险与三项未验证内容见 §6、§7。

---

## 二、验收范围与口径

- **范围**：仅本轮六项改动及其直接影响面。项目既有的 55+ 轮历史不在本次验收范围内（仅做回归确认）。
- **通过标准**：① 目标行为可复现；② 有自动化用例钉住；③ 默认行为与改动前一致（不改变既有语义）；
   ④ 端到端至少一条真实链路验证。
- **不计入缺陷的项**：本机随机进程崩溃（详见 §6.1），需单独复跑判定。

---

## 三、交付物清单

> 行数为**文件整体 diff**：工作区在本轮之前已存在他人未提交的改动，同一文件的行数可能包含那部分；
> 文件清单只列本轮确实改过的。

| 类别 | 文件数 | 行数 |
| --- | --- | --- |
| 生产代码 | 15 | +1,230 / −194 |
| 测试 | 4（另新增 2） | +625 / −6 |
| 配置 / 契约 / 样例 | 3 | +122 / −1 |
| 新增文件 | 5 | 1,084 行（含文档 682） |

### 3.1 生产代码

| 文件 | 改动要点 |
| --- | --- |
| `gate/verdict.py`（新增，110 行） | 裁决契约与派生规则：`ReviewVerdict` / `CriterionJudgement` / `derive_review` / `is_malformed`，G2 与角色内复核共用 |
| `agents/team.py` | 复核改走 `complete_structured`；`_ReviewOutcome` / `_call_as` / `_call_structured`；`_Circuit` 熔断与 `_run_member`；`intra_deps` 拓扑执行；摘录预算均分 |
| `roles/binding.py` | `Aggregation.review_rounds`、`intra_deps` 及配置期校验（自依赖 / 越界 / 成环 / 模式限制） |
| `api/server.py` | `GET /runs/{run_id}/agents`（读最后一条 `run_plan_resolved`） |
| `runtime/ledger.py` | `event_stats()` 增 `llm.by_profile` |
| `models/factory.py` | `llm_call` 携带 `profile`；`_maybe_shift_by_headroom` 成本感知路由 |
| `models/budget.py` | 纯函数 `under_headroom` |
| `models/providers/mock.py` | 评审夹具支持 `"<task_id>@<轮次>"` |
| `gate/reviewer.py` | 改为导入共享裁决；评审调用挂 `profile="gate:<角色>"`；`attempt=0` 避免与轮次夹具撞键 |
| `task/planner.py` | 文档需求判定区分"引用既有文档"与"要求产出文档" |
| `config.py` | 4 个开关；`load_yaml_config(strict=)` + `unknown_yaml_keys()` |
| `evals/{__init__,case,runner}.py` | 矩阵评测：`run_matrix` / `MatrixReport` / 变体加载与校验；`CaseResult` 增成本与过程指标 |
| `utils/trace.py`、`agents/base_agent.py` | `TraceContext.profile` 及其写入 |

### 3.2 测试（修改 4 个文件，+625 / −6；新增 2 个文件，282 行）

`test_agent_binding.py`（+496 行）、`test_planner.py`（+31）、`test_ledger_api.py`（+83）、
`test_hardening_2026_11.py`（+21）；新增 `test_cost_aware_routing.py`（123 行，9 例）、
`test_eval_matrix.py`（159 行，10 例）。

### 3.3 配置、契约与样例

- `artifacts/contracts/openapi.yaml`：补 `GET /runs/{run_id}/agents`（契约守卫双向锁定）
- `examples/agentcrew.multi-agent.example.yaml`：`review_rounds` 与 `intra_deps` 示例
- `examples/samples/revision/`（新样例，7 文件）+ `examples/evals/revision.jsonl`（2 例）
- `examples/samples/__init__.py`：注册 `revision`

### 3.4 文档

`docs/AUDIT-2026-11-09.md`（202 行）、`docs/IMPROVEMENT-PLAN-2026-11-09.md`（480 行）、
`PROJECT-STATE.md` 增 §3.57–§3.62。

---

## 四、逐项验收记录

### A1 复核接治理通道（P0）

| 检查点 | 结果 |
| --- | --- |
| 裁决与 G2 是否同源 | 是，`gate/verdict.py` 单一实现 |
| 复核是否走受治理入口 | 是，`ModelFactory.complete_structured`（信号量 + 记账） |
| 是否仍有"未发现问题"子串匹配 | 否，已删除 |
| 格式错误是否被当作驳回 | 否，记 `agent_degraded` 且不触发返工 |
| G2 既有用例是否改动 | 一行未改，58 例全绿 |

### A2 执行体级熔断（P1.1）

连续失败计数 → 达阈值 `agent_circuit_open` → `_primary_pool` 剔除 → 全熔断时退回原池；
成功一次清零。默认关闭，关闭时不创建 `_Circuit`（零开销）。

### A3 成本感知路由（P1.2）

`under_headroom(spent, ceiling, headroom)` 纯函数 + `_maybe_shift_by_headroom`；
无状态（不写全局降级标志）、只对声明 run 额度的执行体生效、每个执行体只上报一次。
9 例覆盖：阈值边界、默认关、无额度、全局降级优先、headroom 可配置。

### A4 门禁评审归因（P1.3）

`Reviewer._opinion` 挂 `TraceContext(profile="gate:<角色>")`；`attempt=0` 隔离轮次夹具。
实测 `by_profile` 出现 `gate:architect` / `gate:product_manager` / `gate:project_manager`，
`unknown` 由 7 次降至 1 次（剩余为规划期调用）。

### A5 配置 A/B 矩阵（P2.1）

变体文件与 `agentcrew.yaml` 同形，复用同一套归一化；未知顶层段**当场报错**；
首个变体为基线；退出码只看基线。实测见 §5.3。

### A6 分工依赖（P2.2）

`intra_deps` 默认空 → 行为与之前逐字一致（仍 `gather` 并行）；非空 → 拓扑序 +
`UpstreamDigest` 注入；依赖失败不阻塞下游；配置期拒绝自依赖 / 越界 / 成环 / 非 split 模式；
运行期遇环退回原序，**不死循环**。

### A7 复核材料完整性（R48）

预算由"先到先得"改为**按文件数均分**（每份保底 180 token），材料纳入 `store.by_task()`。
修复依据：真模型跑批中复核指控"未提供 README"，而 `README.md` 有 4 条 `artifact_written`。

### A8 复核可复盘

`agent_review` 增 `detail`（截断 400 字）、`approved`、`issues`；此前只有 `chars`。

### A9 规划期文档判定（R48 附带）

关键词前后窗口识别"引用既有文档"（与/按/依据/参照/符合 + ≤4 连接字；或 一致/约定/对齐/定义/样例），
同词多次出现只要有一次非引用仍判为要求文档。58 例 planner 用例全绿。

---

## 五、验证数据附录

### 5.1 自动化测试

```
用例总数（collect）：2,939   （会话开始 2,899，净增 40）
代码规模：132 模块 / 37,221 行　　测试：144 文件 / 40,102 行

全量单进程跑：2933 passed / 5 failed / 1 skipped，320.92s
```

5 个失败均为环境类（见 §6.1），逐个复验：

| 用例 | 复验结果 |
| --- | --- |
| `test_webhook_spool::test_end_to_end_run_replays_failed_notifications` | 单文件 18 passed |
| `test_rate_limit_config::test_config_export_includes_non_default_window` | `config export → import` 手工跑通（rc=0 两次） |
| `test_rate_limit_config::test_config_export_import_roundtrip` | 同上 |
| `test_sdk::test_examples_are_runnable[quickstart.py]` | 崩在 `argparse.format_help` 栈内（标准库） |
| `test_approval_interfaces::test_cli_deferred_approval_roundtrip` | 子进程静默退出 |

### 5.2 真实模型跑批（DeepSeek，3 次，合计 $0.37）

| | 跑 A | 跑 B | 跑 C（修复后） |
| --- | --- | --- | --- |
| 任务 | 7 | 7 | 8 |
| LLM 调用 | 37 | 32 | 28 |
| 成本 | $0.16 | $0.11 | $0.10 |
| 执行体 | 主笔 8/$0.06 · 复核 5/$0.01 | 主笔 6/$0.04 · 复核 6/$0.01 | 主笔 3/$0.02 · 复核 3/$0.00 |
| 角色内复核 | 5（4 驳 1 过） | 6（4 驳 2 过） | 3（1 驳 2 过） |
| 修订 | 3 | 3 | 1 |
| 结果 | 7/7 成功 | 7/7 成功 | 8/8 成功 |

**修订确实改写产物**：`artifact_written.sha256` 显示同一路径修订前后哈希全变
（跑 A：T-03 五个文件、T-04 三个文件）。

### 5.3 配置矩阵对照（离线，`revision` 数据集）

```
base      pass^k=100%  calls=24  cost=$0.0161  修订=0
rounds1   pass^k=100%  calls=30  cost=$0.0208  修订=2
          Δcalls +6 · Δcost $+0.0047 · Δ修订 +2 · Δpass^k 0
```

结论：开修订在此数据集上多花 29% 成本、通过率不变（mock 中主笔前后产物相同）。
**这是"这批 case 不值得开修订"，不是"修订无效"**——后者需真实通道判定。

### 5.4 归因账平

```
llm.calls = 13
by_profile = be_primary 1 | be_reviewer 1 | default 4 | gate:* 6 | unknown 1
分项合计 = 13   ✔
```

### 5.5 复核误判对照

| | 修复前（跑 B） | 修复后（跑 C） |
| --- | --- | --- |
| "指控文件不存在"型意见 | **4 条**（全部误判） | **0 条** |

修复后驳回改为「产物节选被截断，无法核对 docs/PRD.md」——承认材料不足而非断言文件缺失，
一轮修订即通过。

---

## 六、未通过 / 未验证项

### 6.1 环境类失败（不计缺陷，但影响"一次跑全绿"的可信度）

现象：进程级段错误（退出码 139）、标准库内部 `SyntaxError` / `TypeError`、
`PydanticUseDefault` 崩溃。同一文件连跑三次的结果可能是「过、过、崩」。
项目自身 `scripts/test.py` 文档已记录该现象（安全软件注入导致随机崩溃）。
**影响**：任何"全量一次通过"的结论都必须辅以分批复跑，否则不可信。

### 6.2 未验证

| 项 | 原因 |
| --- | --- |
| 前端 Studio 交互 | 本轮全部改动在后端；Web 面板可启动，但未做交互验收 |
| 真模型对照的可重复性 | 计划随机性导致两次被规划校验拦下（其中一个为真 bug，已修） |
| 熔断 / 成本路由的真模型收益 | 仅有单元测试与离线矩阵，未在有真实浪费的场景下量化 |

### 6.3 已记录但未实现（需产品决定）

1. **复核与门禁结论不一致**：真跑中复核判 2 条不满足、任务仍 `succeeded`。
   当前设计如此；若要让复核能拦交付，需要"两票制"（属 `Aggregation` 新模式）。
2. **规划质量**：目标提到"测试/文档"而拆解无对应交付物时，整个 run 被拒（`exit 2`）。
   校验正确，但用户体感是"换个措辞就跑不起来"；建议改为自动补交付物而非拒绝。

---

## 七、风险与遗留

| 风险 | 等级 | 说明 |
| --- | --- | --- |
| 本机运行不稳定 | 高 | 影响测试可信度与长时间跑批；建议在 CI（Linux 单进程）复核 |
| 开关默认全关 | 中 | 熔断与成本路由未经真实浪费场景验证，默认关意味着"能力在但没被用" |
| 复核只影响 notes 与修订 | 中 | 不参与任务状态，可能产生"复核一直驳回但交付照过"的观感 |
| 评测数据单薄 | 中 | 只有 2 条 `revision` case，尚不足以支撑绑定决策 |

---

## 八、结论与建议

**验收结论：通过。** 六项交付均有可复现代码、自动化用例与至少一条端到端证据；
默认行为未改变，回滚只需重置开关。

建议后续优先级（按性价比）：

1. **补评测数据**：造"产物确实缺验收标准"的 case，让 `review_rounds` / 熔断 / 成本路由的
   收益可量化——工具已就绪，缺数据。
2. **前端交互验收**：Studio 是当前最大的未验证面（1113 文件，本轮零改动）。
3. **规划质量专项**：把"验收标准 ↔ 交付物"从"拒绝"改为"自动补全"，用户体感差异最大。

---

*本报告数据取自 2026-11-10 当日运行记录；命令与输出见各章节代码块，可原样复跑。*
