"""C3：G1 执行校验档——「产物能不能跑」变成门禁结论，失败走既有返工通道。

三个必须成立的点：

1. **只跑任务自己交付的测试**：把产物铺进隔离目录，pytest 跑那里的
   ``test_*.py``；一条测试都没有时不假装通过（记 ``skipped``）。
2. **失败并入 G1**：复用 ``ReviewRecord`` 的 issue/fix_hint 与 ``REWORK`` 状态机，
   **不新增第二条返工路径**（多一条路径就多一套额度与收敛语义）。
3. **默认关闭**：没注入 verifier 时一条 ``g1_exec_check`` 都不该出现，行为与
   C3 之前逐字一致。
"""

from __future__ import annotations

from pathlib import Path

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.gate.execute_verify import ExecutionVerifier
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard
from agentcrew.tools.sandbox import ExecutionResult, LocalBackend, SandboxError

GOOD_IMPL = "def add(a, b):\n    return a + b\n"
BAD_IMPL = "def add(a, b):\n    return a - b\n"
GOOD_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def _workspace(tmp_path: Path) -> tuple[Ledger, ArtifactStore, Task, list[ArtifactSpec]]:
    run_dir = tmp_path / "run"
    ledger = Ledger(run_dir / "ledger.jsonl", run_id="R")
    store = ArtifactStore(run_dir / "artifacts", run_id="R", ledger=ledger)
    specs = [
        ArtifactSpec(path="calc.py", kind="python", description="实现"),
        ArtifactSpec(path="test_calc.py", kind="python", description="用例"),
    ]
    task = Task(
        id="T-1", title="加法模块", description="实现 add 并写用例",
        role="backend_dev", acceptance_criteria=["add 正确"],
        deliverables=specs,
    )
    return ledger, store, task, specs


def _verifier(tmp_path: Path, ledger: Ledger, **kwargs) -> ExecutionVerifier:
    return ExecutionVerifier(
        backend=kwargs.pop("backend", LocalBackend()),
        workspace_root=tmp_path / "run" / "verify",
        ledger=ledger,
        timeout=kwargs.pop("timeout", 60),
    )


def _publish(store: ArtifactStore, task: Task, specs, impl: str,
             test: str | None = GOOD_TEST) -> None:
    store.commit("calc.py", impl, spec=specs[0], task=task)
    if test is not None:
        store.commit("test_calc.py", test, spec=specs[1], task=task)


async def test_verify_passes_when_tests_pass(tmp_path):
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, GOOD_IMPL)
    findings = await _verifier(tmp_path, ledger).check(task, store, round_no=1)
    assert findings == []
    event = list(ledger.events("g1_exec_check"))[-1]
    assert event.data["status"] == "passed"
    assert event.data["test_files"] == 1
    assert event.data["backend"] == "local"


async def test_verify_returns_issue_when_tests_fail(tmp_path):
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, BAD_IMPL)
    findings = await _verifier(tmp_path, ledger).check(task, store, round_no=1)
    assert len(findings) == 1
    issue, hint = findings[0]
    assert issue.startswith("执行校验失败")
    assert "1 failed" in issue
    # 细节进 fix_hint：返工方读 fix_hints，报错原文必须原样带过去
    assert "assert" in hint
    assert "修正实现或测试" in hint
    event = list(ledger.events("g1_exec_check"))[-1]
    assert event.data["status"] == "failed"
    assert event.data["exit_code"] != 0


async def test_verify_skips_when_no_tests(tmp_path):
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, GOOD_IMPL, test=None)
    findings = await _verifier(tmp_path, ledger).check(task, store, round_no=1)
    assert findings == []
    event = list(ledger.events("g1_exec_check"))[-1]
    assert event.data["status"] == "skipped"
    assert event.data["reason"] == "no_tests"


async def test_verify_reports_backend_error_as_issue(tmp_path):
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, GOOD_IMPL)

    class _Broken(LocalBackend):
        name = "docker"

        async def run(self, argv, *, workdir, timeout):  # noqa: ANN001
            raise SandboxError("docker 不可用")

    findings = await _verifier(tmp_path, ledger, backend=_Broken()).check(
        task, store, round_no=1)
    assert findings and "执行校验无法运行" in findings[0][0]


async def test_verify_replaces_stale_workspace(tmp_path):
    """上一次校验留下的文件不能影响这一次（否则结论会被历史污染）。"""
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, GOOD_IMPL)
    stale = tmp_path / "run" / "verify" / task.id
    stale.mkdir(parents=True)
    (stale / "test_stale.py").write_text(
        "def test_stale():\n    assert False\n", encoding="utf-8")
    findings = await _verifier(tmp_path, ledger).check(task, store, round_no=1)
    assert findings == []
    assert not (stale / "test_stale.py").exists()


async def test_verify_timeout_is_reported(tmp_path):
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, GOOD_IMPL)

    class _Slow(LocalBackend):
        async def run(self, argv, *, workdir, timeout):  # noqa: ANN001
            return ExecutionResult(exit_code=-1, timed_out=True, backend="local")

    findings = await _verifier(tmp_path, ledger, backend=_Slow()).check(
        task, store, round_no=1)
    assert findings and "执行校验超时" in findings[0][0]


# ── 与 Gatekeeper 的接线：复用同一条返工通道 ─────────────────────────────

def _board_with_review(task: Task) -> TaskBoard:
    board = TaskBoard([task])
    for status in (S.READY, S.RUNNING, S.REVIEW):
        board.transition(task.id, status)
    return board


class _NoReviewer:
    async def review(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("G1 已经驳回，G2 不该被调用（短路被破了）")


async def test_gatekeeper_rejects_on_failed_execution(tmp_path):
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, BAD_IMPL)
    board = _board_with_review(task)
    gk = Gatekeeper(
        _NoReviewer(), store, board, gate="none", ledger=ledger,
        prompter=NullPrompter(),
        gate_config={"g2_enabled": False},
        exec_verifier=_verifier(tmp_path, ledger),
    )
    decision = await gk.evaluate(task, round_no=1)
    assert decision.verdict == "reject"
    assert decision.next_status is S.REWORK
    assert any("执行校验失败" in issue for issue in decision.record.issues)
    assert board.get(task.id).rework_count == 1


async def test_gatekeeper_default_off_has_no_exec_events(tmp_path):
    """不注入 verifier 时一条 ``g1_exec_check`` 都不该出现（默认关闭）。"""
    ledger, store, task, specs = _workspace(tmp_path)
    _publish(store, task, specs, BAD_IMPL)
    board = _board_with_review(task)
    gk = Gatekeeper(
        _NoReviewer(), store, board, gate="none", ledger=ledger,
        prompter=NullPrompter(), gate_config={"g2_enabled": False},
    )
    decision = await gk.evaluate(task, round_no=1)
    # 产物结构没问题 → G1 放行（执行失败这件事默认不参与判定）
    assert decision.verdict == "approve"
    assert ledger.count("g1_exec_check") == 0


def test_crew_wires_verifier_only_when_enabled(tmp_path, monkeypatch):
    """Crew 侧接线：``gate_config.exec_verify`` 打开且非 mock 才注入。"""
    from types import SimpleNamespace

    import agentcrew.crew.crew as crew_mod
    from agentcrew.config import Settings
    from agentcrew.crew.crew import Crew

    monkeypatch.setattr(crew_mod, "Reviewer", lambda *a, **k: _NoReviewer())
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_MOCK", "false")
    run = SimpleNamespace(
        # ``_build_gatekeeper`` 先取 run.models 再构造 Reviewer；这里把 Reviewer
        # 整个换成替身，models 只要存在即可。
        models=SimpleNamespace(),
        store=ArtifactStore(tmp_path / "o" / "art", run_id="R"),
        ledger=Ledger(tmp_path / "o" / "l.jsonl", run_id="R"),
        record=SimpleNamespace(run_id="R"),
    )
    enabled = Crew(
        Settings.load(gate_config={"exec_verify": True}),
        prompter=NullPrompter(),
    )._build_gatekeeper(run, TaskBoard([]), bus=None)
    assert enabled._exec_verifier is not None
    assert enabled._exec_verifier.backend_name == "local"

    disabled = Crew(
        Settings.load(gate_config={}), prompter=NullPrompter(),
    )._build_gatekeeper(run, TaskBoard([]), bus=None)
    assert disabled._exec_verifier is None


def test_crew_skips_verifier_on_mock_channel(tmp_path, monkeypatch):
    """mock 通道显式跳过并留痕：夹具是跨任务引用的，铺单任务产物必然 import 失败。"""
    from types import SimpleNamespace

    import agentcrew.crew.crew as crew_mod
    from agentcrew.config import Settings
    from agentcrew.crew.crew import Crew

    monkeypatch.setattr(crew_mod, "Reviewer", lambda *a, **k: _NoReviewer())
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    (tmp_path / "o").mkdir(parents=True, exist_ok=True)
    ledger = Ledger(tmp_path / "o" / "l.jsonl", run_id="R")
    run = SimpleNamespace(
        models=SimpleNamespace(),
        store=ArtifactStore(tmp_path / "o" / "art", run_id="R"), ledger=ledger,
        record=SimpleNamespace(run_id="R"),
    )
    gk = Crew(
        Settings.load(gate_config={"exec_verify": True}),
        prompter=NullPrompter(),
    )._build_gatekeeper(run, TaskBoard([]), bus=None)
    assert gk._exec_verifier is None
    event = list(ledger.events("g1_exec_check_skipped"))[-1]
    assert event.data["reason"] == "mock_channel"
