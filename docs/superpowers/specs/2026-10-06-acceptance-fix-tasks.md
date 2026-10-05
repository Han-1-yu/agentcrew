# AgentCrew 验收问题修复任务

仓库路径：`E:\agentcrew`
测试环境：Python 3.14.7 / Windows 11（项目目标 3.10+，开发验证环境为 3.12.10）

以下是对 `docs/superpowers/specs/2026-10-05-agentcrew-design.md` §20 验收清单逐条实测后发现的问题，请逐一修复。

---

## 问题 1：pytest 在 Python 3.14 上失败（S4 验收项）

### 严重程度：高

### 复现步骤

```bash
cd E:\agentcrew
python -m pytest -p no:cacheprovider -q
```

### 实际结果

```
2 failed, 1198 passed, 1 skipped
```

### 失败 1（一致复现）：`test_deeply_nested_content_reports_instead_of_raising`

文件：`tests/test_contract.py:155`

```python
def test_deeply_nested_content_reports_instead_of_raising():
    assert parse_check("python", "-" * 3000 + "1") is not None
```

**根因**：Python 3.14 的 `ast.parse` 可以解析 3000 层嵌套的一元负号表达式（`-` * 3000 + "1"），而 Python 3.12 会抛 `RecursionError`。`agentcrew/artifacts/contract.py` 的 `_check_python` 捕获 `RecursionError` 并返回错误消息，但在 3.14 上不抛异常，`parse_check` 返回 `None`（视为合法），断言失败。

验证：
```bash
python -c "import ast; ast.parse('-' * 3000 + '1'); print('parsed OK')"
# Python 3.14: parsed OK
# Python 3.12: RecursionError
```

**预期修复方向**：测试应适配跨版本行为。要么在 `_check_python` 中主动检测嵌套深度（不依赖解释器是否抛 RecursionError），要么修改测试使其在 3.13+ 上接受 `None`（但这会削弱"恶意深层嵌套输入必须被拦截"的安全保证）。建议前者：在 `contract.py` 中对 Python 代码做嵌套深度上限检查，与 JSON/YAML/TOML 的处理保持一致（它们也有深度上限）。

注意：同测试中 JSON/TOML/YAML 的三个断言（`"[" * 3000` 等）在 3.14 上通过，只有 Python 分支失败。

### 失败 2（偶发，仅全量套件出现）：测试污染

全量运行时出现一个测试名 `test_todo_marker_is_still_detected_in_long_content`，但该名称**不存在于任何测试文件中**。单独运行 `tests/test_contract.py` 时只有失败 1，不出现此失败。

错误内容：
```
AssertionError: assert False
 +  where False = has_placeholder((('x = 1\n' * 200) + '# TODO: 实现真正的逻辑\n'))
```

这段输入 `('x = 1\n' * 200) + '# TODO: 实现真正的逻辑\n'` 也不存在于 `test_contract.py` 的任何测试中。

**根因推测**：某个测试模块修改了全局状态后未正确清理（可能是 `sys.setrecursionlimit`、monkeypatch、或 `agentcrew.artifacts.contract` 模块级变量），导致后续测试模块中的 `has_placeholder` 行为异常。需要定位是哪个测试模块造成的污染。

**复现提示**：
```bash
# 单独运行不失败
python -m pytest tests/test_contract.py -p no:cacheprovider -q
# 全量运行才失败
python -m pytest -p no:cacheprovider -q
```

建议用 `pytest -p no:cacheprovider --tb=long -k "todo_marker"` 定位，或二分法逐步缩小测试范围找到污染源。

---

## 问题 2：10 个空 `__init__.py` 文件（原提示词七.1）

### 严重程度：低

### 现状

以下文件均为 0 字节：
- `agentcrew/artifacts/__init__.py`
- `agentcrew/context/__init__.py`
- `agentcrew/gate/__init__.py`
- `agentcrew/models/__init__.py`
- `agentcrew/models/providers/__init__.py`
- `agentcrew/roles/__init__.py`
- `agentcrew/runtime/__init__.py`
- `agentcrew/task/__init__.py`
- `agentcrew/utils/__init__.py`
- `tests/__init__.py`

原提示词七.1 要求"所有文件代码完整，无空文件"。虽然空 `__init__.py` 是 Python 惯例，但严格按验收标准不通过。

**预期修复方向**：在每个 `__init__.py` 中添加模块 docstring，说明该包的职责。例如：
```python
"""产物契约、解析与安全落盘（spec §5/§9）。"""
```
这既满足"非空"要求，又改善了文档。不需要添加导入逻辑（避免循环导入）。

---

## 问题 3：S7 自定义角色在 mock 模式下不被实际分配任务

### 严重程度：中

### 现状

通过 `agentcrew.yaml` 的 `roles_dir` 可以成功加载自定义角色（验证：注册后 catalog 有 9 个角色），完整 mock run 也能退出码 0。但 mock 模式下的计划来自预写 fixture `examples/fixtures/plan_todo.json`，该计划只包含 8 个内置角色的任务，**自定义角色永远不会被分配任务**。

因此 S7"通过 agentcrew.yaml 新增一个角色并跑通"只验证了**加载链路**，没有验证自定义角色能被 PM 拆解时选中、被调度执行、被门禁验收的完整链路。

### 预期修复方向

在 `tests/` 中新增一个端到端测试：构造一个包含自定义角色任务的临时 plan fixture，用 `monkeypatch` 替换 `DEFAULT_FIXTURES_DIR`，跑完整 mock run，断言自定义角色的任务被执行且产物落盘。这能真正验证"角色是数据，引擎零改动"的承诺。

参考已有测试：`tests/test_agents_contract.py::test_pm_prompt_menu_comes_from_live_catalog` 验证了自定义角色进入 PM 提示词，但没有端到端执行。

---

## 问题 4：README 版本声明与设计规格不一致

### 严重程度：低

### 现状

- `README.md` 第 100 行："要求 Python 3.12（开发与验证环境为 3.12.10 / Windows 11）"
- 设计规格 `docs/superpowers/specs/2026-10-05-agentcrew-design.md` 第 6 行："目标运行时：Python 3.10+（开发环境为 3.12.10）"
- `pyproject.toml` 中的 `requires-python` 需要核实

两处声明矛盾。如果目标确实是 3.10+，则问题 1 必须修复（3.14 也是 3.10+）。如果只支持 3.12，应在所有地方统一并在 `requires-python` 中声明上限。

**预期修复方向**：统一版本声明。建议修复问题 1 后保持 3.10+ 的声明（更宽容），并在 CI 中增加 3.10/3.11/3.12/3.13/3.14 的矩阵测试。

---

## 验收标准

修复完成后，以下命令必须全部通过：

```bash
cd E:\agentcrew

# 1. 全量测试零失败（Python 3.14 上）
python -m pytest -p no:cacheprovider -q
# 预期: 1200 passed, 0 failed (或与修复前的 passed 数一致 + 新增测试)

# 2. 单独运行 test_contract 也零失败
python -m pytest tests/test_contract.py -p no:cacheprovider -q

# 3. 全量运行两次结果一致（排除偶发污染）
python -m pytest -p no:cacheprovider -q
python -m pytest -p no:cacheprovider -q

# 4. 没有空文件
# (在 agentcrew/ 和 tests/ 下不应有 0 字节的 .py 文件)

# 5. mock 端到端仍通过
python main.py run --demo --mock --auto --no-live --output-dir output/verify
# 预期: 退出码 0
```

## 不要做的事

- 不要运行需要 API Key 的真实模型测试
- 不要修改 `examples/fixtures/` 中的预置回复（它们是 mock 数据，改动会影响其他测试）
- 不要降低测试覆盖率来换取绿灯
- 不要改动 §20 验收清单本身（那是规格文档）
