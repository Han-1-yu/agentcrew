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

## 七、Git 提交结果（已执行）

原来的 168 个文件已全部落地，**工作区干净（0 项待提交）**，内容批次共 7 个 commit：

| # | SHA | 内容 | 文件数 |
| --- | --- | --- | ---: |
| 1 | `ef9f821` | feat(v7): 脱敏 / 路由 / 亲和性 / 配额 | 13 |
| 2 | `46af647` | refactor(agentcrew): 拆策略模块 + 契约层断反向依赖 | 21 |
| 3 | `adb0bde` | feat(r2): 样例补齐 / 并发基准 / 稳定性测试 | 28 |
| 4 | `f9a52e6` | refactor(studio): 49 个 ESM 模块 + 铜色设计系统 | 约 90 |
| 5 | `87c136d` | docs: 交接/验收/基线报告 + 数据契约参考副本 | 12 |
| 6 | `f1a6ddf` | chore: 忽略本地调试归档与打包过程产物 | 2 |
| 7 | `e3a41b1` | docs(baseline): 记录提交结果与 push 阻断原因 | 1 |

> 另有若干仅修正本文档计数的收尾 commit，不涉及代码。

**相对交接文档四步计划的四处修正**（照抄原计划会漏文件）：

1. 批次 2 补入 `artifacts/contract.py`、`runtime/board_view.py` 及其两个回归测试 —— 文档清单没有，但属 R1 的健壮性修复（解析器异常兜底、`TERM=dumb` 降级）。
2. 批次 5 补入 `studio/zip.js` 与 `studio/probe-edge.js` —— **被 `main.js` 直接 `require`**，漏提交会直接让 Studio 起不来；另补 `AgentCrewStudio.bat`。
3. `artifacts/reference-run/` 确认是 `DATA-CONTRACT-REFERENCE.md` 明确引用的数据副本，属证据材料，随批次 5 提交。
4. `studio/probe-out/`、`_src_inventory.csv`、`zip-verify.zip` 确认为过程产物，进 `.gitignore` 而非提交。

**逐提交可构建验证**：对 7 个 commit 逐个 `git worktree add` 检出并执行 `import main, agentcrew`，**全部 import-ok**（交接文档担心的「单独检出不可构建」没有发生）。

**提交后复跑全量**：`1946 passed / 1 skipped in 129.41s`，与提交前一致。
**Studio 护栏**：`node tools/check-architecture.js` → 49 个 JS 模块 / 32 个样式分片，全合规。

---

## 八、未执行项：push 被环境网络阻断

```bash
$ git ls-remote --heads origin
fatal: unable to access 'https://github.com/Han-1-yu/agentcrew.git/':
       schannel: server closed abruptly (missing close_notify)
$ curl -x $HTTPS_PROXY https://github.com/...
curl: (7) CONNECT tunnel failed, response 502
```

- 当前沙箱代理**放行 PyPI 镜像（200）、拒绝 github.com（502）**，与代码和凭据无关，是网络策略。
- 因此 **CI 首跑尚未触发**，3.10–3.13 矩阵仍是未知数。
- 需要你在**能访问 GitHub 的本机终端**执行：

```bash
cd E:/agentcrew
git push origin master        # 一次推送全部本地提交（原 133 个 + 本次接手批次）
```

推送后到 Actions 页面看矩阵；若低版本报错，把日志发我，我按兼容性修。

---

## 九、下一步建议顺序

1. **轮换 DeepSeek Key** → `doctor` 转绿（唯一的外部依赖）。
2. **在本机 push** → 触发 CI 首跑（这是最有信息量的一次验证）。
3. 清 P2 快赢四项（工具 Schema 强校验 / MCP transport 关闭 / 功能测试报告 / CI 绿）。
4. 提交复验材料（三个 P1 已整改完，只差复验）。

---

## 十、本次新增/改动清单

| 文件 | 说明 |
| --- | --- |
| `.venv-handover/` | 新建隔离环境（自带 `.gitignore`，不污染工作区计数） |
| `HANDOVER-BASELINE-2026-11-01.md` | 本文件（已随批次 5 提交） |
| `output/20261101-1735*/` | 三个样例的运行证据（`output/` 已被忽略） |
| 7 个 commit | 见 §7 |

工作区当前 **0 项待提交**（`.venv-handover` 由 venv 自带 `.gitignore` 自我忽略）。

---

## 十一、真实模型通道验证（2026-11-01 17:46–18:06）

Key 更新后 `doctor` 6 项全通过（`provider:deepseek:deepseek-chat: healthy（807ms）`）。
随后连跑三次真实 DeepSeek 端到端，**每次都暴露一个 mock 结构上无法暴露的缺陷**：

| 轮次 | 结果 | 卡点 | 处置 |
| --- | --- | --- | --- |
| realrun9 | 5 成功 / 1 失败 / 1 阻塞 | T6 的验收标准要求「README 与 `logstat/cli.py` 一致」，G2 只渲染本任务产物 → 三轮驳回 → FAILED | 修复：评审材料补「参考材料」节 |
| realrun10 | 4 成功 / 1 失败 / 4 阻塞 | 动态子任务 `T-DYN-001` 抢占父任务契约里的 `logstat/__init__.py`，父任务落盘被冲突检测拒掉，3 次 attempt 烧光 | 修复：子任务入板前剔除已契约路径 |
| realrun11 | **9/9 全通** | — | 两个修复同时生效 |

realrun11 明细（`output/20261101-180100-2d90`）：$0.15、4m40s、32 次 LLM 调用、
返工 4 次（T2×1 / T4×1 / T5×2）、G1 拦截 1、G2 驳回 3 后全部收敛、**13 个产物 0 哈希异常**。

两处 A/B 证据：

- **T6（README）**：修复前三轮全驳回；修复后**第 1 轮即 approve，score 0.95，零返工**。
- **动态子任务**：realrun11 里 `subtask_conflict` 触发 3 次、`subtask_rejected` 3 次 ——
  被拦下的正是 `tests/test_core.py`、`tests/test_cli.py`、`docs/test-report.md`
  这三个 T5 的契约产物。若没有这道闸，T5 会重演 realrun10 的 FAILED。

> 结论：**真实模型通道可用**，历史 S3 遗留问题（"真实模型下还差最后一公里"）已不再成立。

## 十二、CI 前必须知道：lint 基线不是绿的

`ruff` 在 CI 里是**不钉版本**安装的（`pip install ruff mypy`），所以首次推送会拿到最新版：

| 项 | 实测 |
| --- | --- |
| `ruff check .` | **90 个违规**（41 F401 / 24 I001 / 9 UP037 / 9 F841 / 4 B007 / 1 E741 / 1 F541 / 1 B905） |
| 其中可自动修 | 75 个 |
| 已修的真错误 | 1 个：`base_agent.py` 的 f-string 同引号嵌套（3.12+ 语法，**3.10/3.11 导入即 SyntaxError**，已改为单引号） |

也就是说：**即使代码逻辑全对，首次 CI 的 3.10–3.13 四档也会全部红在 `ruff` 这一步**，
而且 3.10/3.11 那两档在黑 lint 之前就已经死在 `SyntaxError`（这个已修）。

建议单独一批处理，别和功能改动混在一起：

```bash
ruff check . --fix          # 75 个可自动修，修完必须跑全量测试
ruff check .                # 剩 15 个手动（主要是 F841 未使用变量 / B007 / E741）
```

同时把 `ruff` 钉版本（例如 `ruff==<实测通过版本>`）写进 CI，否则今天绿明天红。
