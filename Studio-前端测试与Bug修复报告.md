# AgentCrew Studio 前端测试与 Bug 修复报告

- 测试对象：AgentCrew Studio（Electron 桌面端，目录 `E:\agentcrew\studio`）
- 测试日期：2026-10-11
- 测试方式：内置自动化回归（selftest / e2e）+ 新增边缘用例探针（probe）+ 静态代码审查，全部在真实 Electron 渲染进程的真实 DOM 上执行，附自动截图
- 结论：发现并修复 **3 个确定性 Bug**；修复后 selftest 16/16、e2e 28/28、probe 24/24 全部通过，无残留进程

---

## 一、测试执行结果总览

| 测试层 | 覆盖内容 | 修复前 | 修复后 |
| ------ | -------- | ------ | ------ |
| selftest（`electron . --selftest`） | 16 项渲染/状态/主题基础自测，截图 11 张 | 16/16 | **16/16** |
| e2e（`electron . --e2e`） | 28 项九页导航、详情、运行管理、供应商、计划、主题端到端，截图 16 张 | 28/28（含 1 项固化了错误预期的断言） | **28/28** |
| probe（`electron . --probe`，本轮新增） | 24 项：九页渲染冒烟 + 快捷键映射 + 模态焦点 + 下拉关闭 + 空/非法输入 + 连点 + 缩放极限 + 筛选隔离 + 主题循环 + 预算夹取 | 11/15（九页冒烟加入后为 22/24，其中 2 项为探针自身预期错误） | **24/24** |

九页渲染冒烟（修复后实测每页可见宿主的 DOM 节点数，均无错误弹窗）：

| 页面 | dashboard | tasks | dag | agents | gates | artifacts | events | providers | settings |
| ---- | --------- | ----- | --- | ------ | ----- | --------- | ------ | --------- | -------- |
| 节点数 | 928 | 372 | 94 | 352 | 135 | 12 | 825 | 705 | 167 |
| 错误弹窗 | 无 | 无 | 无 | 无 | 无 | 无 | 无 | 无 | 无 |

---

## 二、确认并修复的 Bug 清单

### BUG-1　数字快捷键 8/9 与侧边栏视觉顺序互换（定级：P2）

- **现象**：侧边栏从上到下第 8 项是「供应商」、第 9 项是「设置」；但键盘按 `8` 跳到「设置」、按 `9` 跳到「供应商」，与界面编号相反。
- **复现**：Dashboard 下派发 `keydown key=8` → 当前页为 settings；派发 `key=9` → 当前页为 providers。
- **根因**：`renderer.js` 全局 keydown 处理内的页面数组末两项写反：
  - 修复前：`['dashboard','tasks','dag','agents','gates','artifacts','events','settings','providers']`
  - 侧边栏权威顺序（`index.html`）：`…events, providers, settings`
- **影响**：用户按界面位置使用数字快捷键时进入错误页面；原 e2e 只校验「9→供应商」，因 providers 恰在数组 index 8 而长期漏检。
- **修复**：将数组改为 `['dashboard','tasks','dag','agents','gates','artifacts','events','providers','settings']`。
- **修复验证**：probe P1 实测 `8→providers、9→settings`；e2e 第 14 项更新为 `4→Agents、8→供应商、9→设置` 并通过。

### BUG-2　模态/详情面板打开时数字快捷键仍切换底层页面（定级：P1）

- **现象**：打开「启动新运行」模态后，按数字键会在遮罩层背后切换页面；关闭模态后用户停留在非预期页面，丢失原上下文。
- **复现**：点击「新建运行」→ 模态打开（`#modal` 无 hidden）→ 派发 `keydown key=4` → 底层 nav 变为 agents。
- **根因**：`renderer.js` keydown 的数字分支与 `Ctrl+N` 分支均未判断是否已有遮罩层打开，缺少焦点守卫。
- **影响**：模态并非真正模态，后台页面被静默切换；`Ctrl+N` 还可能在弹窗上叠加第二个弹窗。
- **修复**：
  1. 新增 `anyOverlayOpen()`：当 `#modal` 或 `#detail-panel` 任一处于打开（不含 hidden）时返回 true；
  2. 数字键分支在 `anyOverlayOpen()` 为真时直接 return，不切换；
  3. `Ctrl+N` 在遮罩层打开时不重复弹窗。
- **修复验证**：probe P2 实测模态打开时按 4，nav 保持 dashboard；P10 详情面板打开/遮罩关闭正常。

### BUG-3　预算/调用次数/亲和 TTL 接受负值，未做下限夹取（定级：P2）

- **现象**：设置页「预算上限 (USD)」填入 `-5` 保存后被原样保留；新建运行模态的预算上限、max-llm-calls 同样接受负数；设置页亲和 TTL 也可被写成负数。
- **复现**：设置页 `#set-maxcost` 填 `-5` → 点击保存 → `getSettings()` 回读 `maxCostUsd = -5`。
- **根因**：保存逻辑使用 `parseFloat(x) || 0` / `parseInt(x) || 7`，仅在 NaN 时回退，未对负数做 `Math.max` 夹取（输入框虽有 `min=0`，但保存路径不强制）。
- **影响**：负预算/负调用次数传入后端会造成预算判定与配额控制语义错误（如预算立即耗尽或绕过限制），负 TTL 无意义。
- **修复**（均为下限夹取）：
  - 设置页：`maxCostUsd: Math.max(0, parseFloat(...) || 0)`、`affinityTtlDays: Math.max(1, parseInt(...) || 7)`；
  - 新建运行模态：`maxCost: Math.max(0, parseFloat(...) || 0)`、`maxLlmCalls: Math.max(0, parseInt(...) || 0)`。
- **修复验证**：probe P15 走真实「保存按钮 + getSettings 回读」，填 -5 后保存值为 0（结束后还原原值）。

---

## 三、经核查「非缺陷」的边界项（避免误报）

下列项在静态审查与探针中被重点怀疑，实测实现正确，**不计入 Bug**：

1. **空目标启动**：未勾 Demo 且目标为空时，启动被行内拦截，`#nr-goal-error` 提示「请输入项目目标，或勾选 Demo 目标」，模态不关闭（probe P5）。
2. **Mock 联动**：勾选 Mock 后 provider/model/自定义模型控件置灰、模型区 opacity=0.45、慢速选项可见；取消后恢复（probe P6、e2e 6b）。
3. **Demo 联动**：勾选 Demo 后目标框置灰并显示内置示例提示，取消恢复（probe P7）。
4. **运行下拉**：点工作区可打开/再点关闭（toggle），点击页面其他处通过 document outside-click 关闭（probe P3/P4）。
5. **连点导航**：连点同一导航 5 次仅产生 1 个 active（probe P8）。
6. **DAG 缩放**：放大生效，连续缩小 20 次下限为 30%（不出现 0%/负缩放），重置回 100%（probe P9）。
7. **详情遮罩**：点节点打开详情、点遮罩在 220ms 动画后关闭（probe P10）。
8. **事件筛选/搜索**：单类型筛选隔离正确（50 行均为该类型），搜索、分页、导出 JSON 齐全（probe P11）。
9. **供应商搜索/分类**：无匹配时卡片为 0 并显示空态，清空恢复 27 张；分类为单选，「全部」恢复（probe P12/P13）。
10. **主题循环**：模式按 `light→dark→system→light` 正确循环并持久化，3 击回到起点；`system` 随操作系统解析（probe P14）。
11. **安全**：`h()/s()` 虚拟节点对字符串走 textContent、`highlightCode` 先转义再包裹，无 XSS/注入；`readArtifact` 含路径越界守卫。
12. **二进制读取**：`main.js readArtifact` 对非文本扩展名返回 `{binary:true}`，渲染端显示「（空文件或二进制）」，**不会乱码**。

---

## 四、变更文件清单

| 文件 | 变更 |
| ---- | ---- |
| `studio/renderer.js` | 新增 `anyOverlayOpen()`；修正数字快捷键页面数组顺序；数字键与 Ctrl+N 增加遮罩焦点守卫；设置页 maxCostUsd/affinityTtlDays 与新建模态 maxCost/maxLlmCalls 增加下限夹取 |
| `studio/e2e.js` | 第 14 项断言由旧的「9→供应商」更正为「4→Agents、8→供应商、9→设置」 |
| `studio/main.js` | 新增 `IS_PROBE` 常量与 `--probe` 分发（测试基础设施，不影响生产路径） |
| `studio/probe-edge.js` | 新增：24 项边缘用例探针，结果与截图输出至 `studio/probe-out/` |

> 本轮仅改动 Studio 前端与前端测试代码，未修改任何后端 Python 源码。

---

## 五、遗留观察项（非阻塞，建议后续迭代）

1. **二进制产物不渲染缩略图**：当前对图片（png/jpg）与压缩包统一显示「（空文件或二进制）」，不展示图片预览，且把「空文件」与「二进制」混为同一提示。建议在 `previewArtifact` 中：对图片类返回 data URL 并用 `<img>` 渲染；空文件与二进制分别给出文案。现有产物全部为文本/代码，无二进制样本，属 P2 增强。
2. **工具入参 JSON Schema 执行前不强校验**、**pytest 残留 1 个 unclosed transport warning（test_mcp_transports）**：均为后端项，不在本轮前端范围，建议后端迭代处理。

---

## 六、复验方式（可复现）

```powershell
cd E:\agentcrew\studio
.\node_modules\.bin\electron.cmd . --selftest   # 期望 16/16
.\node_modules\.bin\electron.cmd . --e2e        # 期望 28/28
.\node_modules\.bin\electron.cmd . --probe      # 期望 24/24，结果见 probe-out/results.json
```

各层结果 JSON 与截图：`studio/selftest/`、`studio/e2e-out/`、`studio/probe-out/`。
