# 接手基线报告（2026-11-01 17:40）

> 目的：把仓库从「交接文档描述的状态」推进到**可复现的接手基线**。
> 本文只记录**实测**结果与**与文档不一致**的地方；不重复交接文档已有内容。
> 未执行任何 Git 写操作（无 commit / 无 push），工作区改动数保持 168 不变。

---

## 一、一句话结论

**引擎与管线完全健康**：全量 `1946 passed / 1 skipped`，三个样例 100% 通过，产物哈希与清单零偏差。
**风险不在代码，在两件事**：Git 提交清单不完整（文档的「65 项」是折叠计数，真实 168 个文件）、真实模型通道因 API Key 失效而不可用。

---

## 二、环境基线

### 2.1 本机真实情况

| 项 | 实测 |
| --- | --- |
| 可用解释器 | **仅一个**：`C:\Users\33793\.workbuddy\binaries\python\versions\3.13.12\python.exe`（`sys.version` 报 3.13.14） |
| 系统级 Python | **无**（无 `py` launcher，无 `C:\Python*`，无 `Program Files\Python*`） |
| `.venv-ci/` | **已失效**：`Scripts\python.exe` 是 shim，指向不存在的 Doubao sandbox 路径 |
| `.venv-handover/` | **本次新建**，从上述解释器创建，依赖装全，可正常使用 |

> 结论：`V7-HANDOFF.md` 里所有 `.venv-ci\Scripts\python.exe ...` 命令**一律不能照抄**，改用 `.venv-handover`。

### 2.2 可复现命令（唯一正确姿势）

```bash
cd /e/agentcrew
PYTHONPATH= ./.venv-handover/Scripts/python.exe -m pytest -q --assert=plain -p no:cacheprovider
PYTHONPATH= ./.venv-handover/Scripts/python.exe main.py run --sample hierarchy --no-live
```

**`PYTHONPATH=` 前缀是必须的**，原因见 §3.1。

### 2.3 依赖版本（requirements.txt 只有下限，实际装到最新）

| 包 | 声明 | 实装 |
| --- | --- | --- |
| openai | >=1.40.0 | **3.42.0** |
| anthropic | >=0.34.0 | **2.3.0** |
| pytest | >=8.0.0 | **9.2.0** |
| pytest-asyncio | >=0.23.0 | **1.4.1** |
| pydantic | >=2.7.0 | 2.14.2 |
| rich | >=13.7.0 | 15.0.0 |

> 值得记一笔：跨了这么多大版本，全量测试仍然全绿 —— 说明代码没有依赖已移除的旧 API。但**环境不可复现**（每次干净安装都可能拉到新的大版本），建议后续把 `requirements.txt` 钉到实测通过的版本区间。

---

## 三、与交接文档不一致的四处（按重要性排序）

### 3.1 「解释器不稳定导致 segfault」——实际是 PYTHONPATH 污染

交接文档 §5.3 把全量 pytest 崩溃归因为「本机 Python 3.13.12 不稳定，重跑通常即过」。实测不是：

| 条件 | 结果 |
| --- | --- |
| 系统 python + 继承 PYTHONPATH（含 WorkBuddy `cli\vendor\shim`） | **收集阶段崩溃**（两次，均崩在 `_pytest/config`） |
| 系统 python + `PYTHONPATH=` 清空 | **1947 项收集成功，0.71 秒** |
| 隔离 venv + `PYTHONPATH=` 清空 | **1946 passed / 1 skipped，164.78 秒** |

根因：WorkBuddy 注入的 `PYTHONPATH` 把一个 shim 目录塞到 `sys.path` 首位，与 pytest 插件加载冲突。
**处理**：跑测试一律加 `PYTHONPATH=`，不需要 `--assert=plain` 也能跑（保留亦无害）。

### 3.2 「65 项改动未提交」——真实是 168 个文件

`git status` 默认把未跟踪目录折叠成一行，所以只显示 65；`-uall` 展开后是 **168**：

| 类别 | 数量 |
| --- | ---: |
| 已修改（M） | 25 |
| 已删除（D） | 2（`studio/renderer.js`、`studio/styles.css`） |
| 未跟踪（??） | 141 |

**交接文档 §5.4 的四步提交清单并不覆盖全部 141 个未跟踪文件**，至少漏了三类，提交前必须做决定：

1. **根级交付文档（4 份）**：`HANDOFF-2026-11-01.md`、`V7-HANDOFF.md`、`AgentCrew-结项验收评审报告.md`、`Studio-前端测试与Bug修复报告.md` —— 建议单独一个 `docs:` commit。
2. **`artifacts/reference-run/`（4 个文件）**：`run.json` / `tasks.json` / `ledger.jsonl` / `decisions.jsonl` —— 需确认是证据材料还是运行残留。
3. **Studio 调试残留（7 个）**：`studio/probe-edge.js`、`studio/probe-out/*`、`studio/zip.js`、`studio/zip-verify.zip`、`studio/_src_inventory.csv`、`studio/AgentCrewStudio.bat` —— **建议加 `.gitignore` 而不是提交**（`zip-verify.zip` 与 `_src_inventory.csv` 明显是过程产物）。

### 3.3 样例任务数：4/6 → 实际 5/7

文档写 `linear` 4 任务、`hierarchy` 6 任务；`main.py samples` 实测为 **5 个 / 7 个**，差额是引擎在计划校验后追加的 `T-FINAL`。数字没错，只是口径不同（计划任务数 vs 运行任务数），读报告时别误判为「样例被改过」。

### 3.4 `.venv-ci` 失效（已在 §2.1 说明）

---

## 四、测试基线

```
1946 passed, 1 skipped in 164.78s (0:02:44)
```

与交接文档声称的 `1946 passed / 1 skipped / 0 failed` **完全一致**，零失败、零 error。
唯一 warning 是 `pytest-asyncio` 的 `asyncio_default_fixture_loop_scope` 未设置（V2 起就存在，不影响结果；顺手在 `pyproject.toml` 里补一行即可消掉）。

---

## 五、端到端基线（三个样例全部离线跑通）

| 样例 | 任务 | 成功/失败/阻塞 | 返工 | G1 拦截 | 耗时 | 产物数 | 哈希校验 | 退出码 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: |
| `linear`（串行） | 5 | 5/0/0 | 0 | 0 | 2s | 6 | ✅ 0 偏差 | 0 |
| `hierarchy`（层级） | 7 | 7/0/0 | 0 | 0 | 3s | 7 | ✅ 0 偏差 | 0 |
| `todo`（混合并发） | 9 | 9/0/0 | 1（T-005） | 1 | 5s | 23 | ✅ 0 偏差 | 0 |

- 产物校验口径：按 `artifacts/MANIFEST.json` 的 `entries[]` 逐条比对存在性与 `sha256`，**全部一致**，三个 run 均有 `DELIVERY.md`。
- `todo` 的 1 次返工是**设计内**的 T-005 演示闭环，不是缺陷。
- 非 TTY 下人工闸门按设计显式降级为 `gate=none` 并打印说明，符合「禁止 fail-open」原则。

证据 run_id：`20261101-173517-6adc`（linear）、`20261101-173522-1275`（hierarchy）、`20261101-173526-1263`（todo）。

---

## 六、阻塞项：真实模型通道不可用

`main.py doctor` 6 项检查，5 项通过，1 项失败：

```
✗ provider:deepseek:deepseek-chat: unavailable：PermanentError: Error code: 401
  Authentication Fails, Your api key: ****fc2e is invalid
```

- 与 `HANDOFF.md` §6 的警告吻合：那个 Key 曾在对话里明文出现过，**应当早已轮换**，现在已确认失效。
- 影响：所有 `--no-live` 之外的验证（真实模型端到端、成本统计、G2 真实评审质量）**当前无法进行**。
- 不阻塞：mock 全链路、全量测试、三个样例、Studio 演示模式。
- 另：`doctor` 提示可选依赖 `aiohttp` 缺失（仅影响 MCP SSE 传输的可选路径，非核心）。

**需要你做的一件事**：到 DeepSeek 控制台签发新 Key，写进 `.env`（该文件已 git 忽略），再跑 `main.py doctor` 确认转绿。

---

## 七、待确认事项（我没有擅自执行）

| # | 事项 | 为什么需要你点头 |
| --- | --- | --- |
| 1 | 按 §5.4 四步 + 我补的第 5 步提交，把 168 个文件落地 | 会改写 Git 历史；顺序错会产生「单独检出不可构建」的 commit |
| 2 | 是否 `push origin master`（133 个提交）触发 CI 首跑 | 对外动作；会把代码推到 GitHub 公开仓库 |
| 3 | CI 若红，是否授权直接改代码修 3.10–3.13 兼容性 | 可能改动核心代码，需明确边界 |

建议的提交批次（在文档四步之后追加）：

```bash
# 5) 交付文档与证据（不含 studio 调试残留）
git add HANDOFF-2026-11-01.md V7-HANDOFF.md AgentCrew-结项验收评审报告.md \
        Studio-前端测试与Bug修复报告.md HANDOVER-BASELINE-2026-11-01.md
git commit -m "docs: 补齐交接、验收与基线报告"

# 6) 调试残留改为忽略（不提交）
printf 'studio/probe-out/\nstudio/_src_inventory.csv\nstudio/zip-verify.zip\n' >> .gitignore
git add .gitignore && git commit -m "chore: 忽略 studio 过程产物"
```

---

## 八、下一步建议顺序

1. **轮换 DeepSeek Key** → `doctor` 转绿（唯一的外部依赖）。
2. **确认提交方案** → 分批 commit → 本地跑一次全量确认无遗漏。
3. **push 触发 CI** → 看 3.10–3.13 矩阵（低版本可能出现语法/API 差异，这是最有信息量的一次验证）。
4. 清 P2 快赢四项（工具 Schema 强校验 / MCP transport 关闭 / 功能测试报告 / CI 绿）。
5. 提交复验材料（三个 P1 已整改完，只差复验）。

---

## 九、本次新增/改动清单

| 文件 | 说明 |
| --- | --- |
| `.venv-handover/` | 新建隔离环境（自带 `.gitignore`，不污染工作区计数） |
| `HANDOVER-BASELINE-2026-11-01.md` | 本文件 |
| `output/20261101-1735*/` | 三个样例的运行证据（`output/` 已被忽略） |

**未改动任何受版本控制的文件**：已跟踪改动仍为 27 项（25 M + 2 D），与开工前一致；未跟踪文件因新增本报告从 141 增至 142，总数 169。
