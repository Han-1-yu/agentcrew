"""Scheduler 调度引擎（Task 28，spec §7.3）。

brief 用例原样保留，只改一处夹具键：``reviews.json`` 用 ``_default`` 而非
``"*"``（与 ``MockProvider._review_text`` 的查找顺序一致 —— 它按
``data[task_id]`` → ``data["_default"]`` 查找；写成 ``"*"`` 会落到占位回复并
触发 Task 26 的「评审器格式错误」放行路径，run 仍能成功但评审夹具的
verdict/score 永不生效。仓库已就此改过一版计划文档，见 commit 738e8d9）。

在其之上补：

- **返工前清空旧产物**（Task 13 carry-forward 的死亡螺旋修复）：多余产物的
  记录会在整轮 run 里挂在 ``store.by_task`` 上，让此后每一次返工都被 G1 的
  「契约外产物」检查驳回 —— 一个多余文件就把任务判死。
- **``peak_concurrency`` 是实测值**：既在满额派发时达到上限，也在任务数少于
  上限时如实回落到真实并发数（不是回读配置）。
- **返工派发不扣 attempts**（Task 8 的额度语义）：``REWORK → RUNNING`` 的
  reason 是 ``rework-dispatched``，与 ``READY → RUNNING`` 的 ``dispatched``
  区分开。
- **取消语义**：软取消让在跑的 worker 自然结束；硬取消取消在跑的 asyncio
  任务并把它们置为 ``CANCELLED``。
- **G2 真跑**：评审夹具的 approve/score 必须真正生效（钉住上面的夹具键修正）。
- **槽位边界是结构性的**：``tests/test_scheduler_deadlock.py`` 的超时兜底对
  「门禁内嵌进任务槽位」其实不敏感（单任务下嵌套不会等死，已实测），所以这里
  再加一条直接读槽位状态的守卫。
"""

import asyncio
import json

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.agents.registry import AgentFactory
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard

GOOD = {"verdict": "approve", "score": 0.95, "issues": [], "fix_hints": []}


def mk(tid, deps=(), role="backend_dev", priority=5):
    return Task(id=tid, title=tid, description=f"写 {tid}", role=role,
                acceptance_criteria=["ok"], dependencies=list(deps),
                priority=priority,
                deliverables=[ArtifactSpec(path=f"{tid}.py", kind="python",
                                           description="d")])


def write_fixture(fx, task_id: str, attempt: int = 1, bad: bool = False):
    d = fx / "responses" / task_id
    d.mkdir(parents=True, exist_ok=True)
    # bad 必须是「围栏正确闭合、内容不合法（Python 语法错误）」：brief 原样的
    # ``broken(`` 不带换行，闭栏与正文挤在同一行 → 未闭合围栏 → 解析失败走
    # READY 重试通道，永远到不了 G1 驳回 → REWORK，测不到返工回路。
    body = "broken(\n" if bad else f"# {task_id}\nX = 1\n"
    (d / f"attempt-{attempt}.md").write_text(
        f"```python file={task_id}.py\n{body}```\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R", secrets=s.secret_values())
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "reviews.json").write_text(json.dumps({"_default": GOOD}), encoding="utf-8")
    models = ModelFactory(s, ledger=lg, fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, store, lg, "目标", catalog)
    rev = Reviewer(models, catalog, store, lg)
    return dict(settings=s, ledger=lg, store=store, fx=fx, models=models,
                catalog=catalog, agents=agents, reviewer=rev, tmp_path=tmp_path)


def build(env, tasks, max_concurrency=4):
    board = TaskBoard(tasks)
    store = env["store"]
    bus = ContextBus("目标", board, store)
    gk = Gatekeeper(env["reviewer"], store, board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    return board, Scheduler(board, env["agents"], gk, bus,
                            ledger=env["ledger"], max_concurrency=max_concurrency)


def build_slow(env, tasks, *, latency_ms, max_concurrency=4):
    """带 mock 延迟的装配：worker 真正在途，用于取消语义（软/硬）测试。"""
    settings = env["settings"].model_copy(update={"mock_latency_ms": latency_ms})
    models = ModelFactory(settings, ledger=env["ledger"], fixtures_dir=env["fx"])
    catalog = env["catalog"]
    store = env["store"]
    board = TaskBoard(tasks)
    bus = ContextBus("目标", board, store)
    reviewer = Reviewer(models, catalog, store, env["ledger"])
    gk = Gatekeeper(reviewer, store, board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    agents = AgentFactory(models, store, env["ledger"], "目标", catalog)
    return board, Scheduler(board, agents, gk, bus, ledger=env["ledger"],
                            max_concurrency=max_concurrency)


async def test_runs_diamond_to_completion(env):
    write_fixture(env["fx"], "A"); write_fixture(env["fx"], "B")
    write_fixture(env["fx"], "C"); write_fixture(env["fx"], "D")
    board, sch = build(env, [mk("A"), mk("B", ["A"]), mk("C", ["A"]), mk("D", ["B", "C"])])
    await sch.run()
    assert all(board.get(t).status is S.SUCCEEDED for t in "ABCD")
    assert board.is_terminal()


async def test_respects_max_concurrency(env):
    for t in "ABCDEF":
        write_fixture(env["fx"], t)
    env["settings"] = env["settings"].model_copy(update={"max_concurrency": 2})
    env["models"] = ModelFactory(env["settings"], ledger=env["ledger"],
                                 fixtures_dir=env["fx"])
    board = TaskBoard([mk(t) for t in "ABCDEF"])
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    agents = AgentFactory(env["models"], env["store"], env["ledger"], "g", env["catalog"])
    sch = Scheduler(board, agents, gk, bus, ledger=env["ledger"], max_concurrency=2)
    await sch.run()
    assert sch.peak_concurrency <= 2


async def test_failure_blocks_downstream(env):
    write_fixture(env["fx"], "A", bad=True)
    for i in (1, 2, 3):
        write_fixture(env["fx"], "A", attempt=i, bad=True)
    write_fixture(env["fx"], "B")
    a = mk("A"); a.max_attempts = 1; a.max_reworks = 0
    board, sch = build(env, [a, mk("B", ["A"])])
    await sch.run()
    assert board.get("A").status is S.FAILED
    assert board.get("B").status is S.BLOCKED


async def test_unrelated_branch_still_completes_after_failure(env):
    for i in (1, 2, 3):
        write_fixture(env["fx"], "A", attempt=i, bad=True)
    write_fixture(env["fx"], "C")
    a = mk("A"); a.max_attempts = 1; a.max_reworks = 0
    board, sch = build(env, [a, mk("B", ["A"]), mk("C")])
    await sch.run()
    assert board.get("C").status is S.SUCCEEDED
    assert board.get("B").status is S.BLOCKED


async def test_rework_loop_uses_second_attempt_fixture(env):
    write_fixture(env["fx"], "A", attempt=1, bad=True)
    write_fixture(env["fx"], "A", attempt=2)
    board, sch = build(env, [mk("A")])
    await sch.run()
    assert board.get("A").status is S.SUCCEEDED
    assert board.get("A").rework_count == 1


async def test_cancel_stops_dispatching_new_tasks(env):
    for t in "ABCDE":
        write_fixture(env["fx"], t)
    # 加一点 mock 延迟：保证 cancel 落在第一个任务执行期间（brief 原版靠 fsync
    # 的真实耗时，在快机器上可能整轮跑完才等到 0.01s 的取消，属于时序竞态）。
    board, sch = build_slow(env, [mk(t) for t in "ABCDE"], latency_ms=50,
                            max_concurrency=1)

    async def canceller():
        await asyncio.sleep(0.01); sch.cancel()

    await asyncio.gather(sch.run(), canceller())
    assert not board.is_terminal()
    assert board.get("E").status is not S.SUCCEEDED


async def test_deadlock_reported_when_no_ready_and_no_running(env):
    board = TaskBoard([mk("A"), mk("B", ["A"])])
    board.transition("A", S.READY); board.transition("A", S.RUNNING)
    board.transition("A", S.REVIEW)
    board.transition("A", S.REWORK)
    board.get("A").status = S.SUCCEEDED          # 人为制造不可达状态
    board.get("B").status = S.PENDING
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"])
    board.get("B").status = S.RUNNING            # 卡住：RUNNING 但引擎无对应 worker
    await asyncio.wait_for(sch.run(), timeout=3)
    assert sch.deadlock_report is not None
    assert "B" in sch.deadlock_report             # 诊断必须点名卡住的任务


async def test_checkpoint_saved_after_each_completion(env, tmp_path):
    write_fixture(env["fx"], "A")
    cs = CheckpointStore.create(tmp_path / "o", project_goal="g",
                                config_snapshot={}, run_id="R1")
    board = TaskBoard([mk("A")])
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"],
                    checkpoint=cs, max_concurrency=4)
    await sch.run()
    assert json.loads((cs.run_dir / "tasks.json").read_text(encoding="utf-8"))


# ── 执行失败：READY 重试通道（不占返工额度）──

def write_unclosed_fixture(fx, task_id: str, attempt: int = 1):
    """未闭合围栏：执行阶段必然抛 ``AgentExecutionError``（走 READY 重试）。"""
    d = fx / "responses" / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"attempt-{attempt}.md").write_text(
        f"```python file={task_id}.py\nbroken(```\n", encoding="utf-8")


async def test_parse_error_retries_without_spending_rework(env):
    """未闭合围栏 → AgentExecutionError → READY 重试（不烧返工额度）。"""
    write_unclosed_fixture(env["fx"], "A", attempt=1)
    write_fixture(env["fx"], "A", attempt=2)
    board, sch = build(env, [mk("A")])
    await sch.run()
    a = board.get("A")
    assert a.status is S.SUCCEEDED
    assert a.attempts == 2 and a.rework_count == 0


async def test_parse_error_exhausts_attempts_then_fails(env):
    """执行错误烧完 ``max_attempts`` → FAILED（``attempts`` 在派发时自增）。"""
    a = mk("A"); a.max_attempts = 2
    write_unclosed_fixture(env["fx"], "A", attempt=1)
    board, sch = build(env, [a])
    await sch.run()
    assert board.get("A").status is S.FAILED
    assert board.get("A").attempts == 2


# ── 死亡螺旋修复（Task 13 的 carry-forward）──

async def test_rework_discards_stale_extra_artifact(env):
    """多余产物在返工派发前被丢弃：任务能靠第二次尝试通过，而不是烧完返工额度。

    没有 ``discard_task`` 时，``A_extra.py`` 的记录会一直挂在 ``store.by_task``
    上，第 2 次尝试产出正确的 ``A.py`` 仍会被 G1 的「契约外产物」检查驳回 ——
    任务最终 FAILED。这里钉的是「一个多余文件不再把任务判死」。
    """
    write_fixture(env["fx"], "A", attempt=1, bad=True)
    write_fixture(env["fx"], "A", attempt=2)
    board, sch = build(env, [mk("A")])
    extra = ArtifactSpec(path="A_extra.py", kind="python", description="d")
    env["store"].commit("A_extra.py", "y = 1\n", spec=extra, task=board.get("A"))

    await sch.run()

    assert board.get("A").status is S.SUCCEEDED
    assert board.get("A").rework_count == 1
    assert [r.path for r in env["store"].by_task("A")] == ["A.py"]
    assert not env["store"].exists("A_extra.py")


async def test_rework_dispatch_does_not_bump_attempts(env):
    """READY 派发扣一次 attempts；REWORK 派发不扣（Task 8 的额度语义）。"""
    write_fixture(env["fx"], "A", attempt=1, bad=True)
    write_fixture(env["fx"], "A", attempt=2)
    board, sch = build(env, [mk("A")])
    await sch.run()
    a = board.get("A")
    assert a.status is S.SUCCEEDED and a.rework_count == 1
    assert a.attempts == 1
    runs = [change.reason for change in a.status_history
            if change.to_status is S.RUNNING]
    assert runs == ["dispatched", "rework-dispatched"]


# ── 停在 REVIEW 的任务必须被重新送进门禁 ──

async def test_review_task_without_a_worker_is_regated(env):
    """没有 worker 的 ``REVIEW`` 任务必须被补派发到门禁，而不是永远停在那里。

    ``repair_statuses`` 刻意保留 ``REVIEW``（产物已完整落盘，只需重跑门禁），
    但调度循环只处理自己派发的 worker 的完成事件、``take_ready`` 又只派发
    ``READY`` / ``REWORK`` —— 缺了这道补派发，一个停在 ``REVIEW`` 的任务没有
    worker、永不终结，它的下游全部卡死（整个 run 判停滞、退出码 4）。
    真实触发路径：硬杀 / 断电 / 文档化的第二次 Ctrl-C，窗口 = 每一次门禁评估。
    """
    write_fixture(env["fx"], "A")
    write_fixture(env["fx"], "B")
    a = mk("A")
    # 产物已在盘上（门禁窗口里被杀的任务，产物是完整的）：G1 只差重新跑一遍
    env["store"].commit("A.py", "# A\nX = 1\n", spec=a.deliverables[0], task=a)
    env["store"].write_manifest()
    board, sch = build(
        env, [a.model_copy(update={"status": S.REVIEW}), mk("B", ["A"])])

    await asyncio.wait_for(sch.run(), timeout=5)

    assert sch.deadlock_report is None
    assert board.get("A").status is S.SUCCEEDED   # 门禁真的被判了
    assert board.get("B").status is S.SUCCEEDED   # 下游因此被解锁
    assert board.get("B").attempts == 1           # 走的是正常执行通道


async def test_regate_dispatch_respects_the_worker_cap(env):
    """补派发门禁不得突破并发上限：``peak_concurrency`` 是报告里读的实测值。

    ``free`` 只算一次就给两个派发循环复用，于是 ``take_ready`` 与 ``_regate_ids``
    各能派满一轮 —— 注册表峰值到 2× ``max_concurrency``，而 ``_regate_ids`` 的
    docstring 声称它「保证 worker 注册表不超并发上限」。真实 Agent 并发仍由任务
    槽位兜住，但报告里那个数会撒谎。
    """
    for t in "ABCDEFGH":
        write_fixture(env["fx"], t)
    review = []
    for tid in "ABCD":
        task = mk(tid)
        env["store"].commit(f"{tid}.py", f"# {tid}\nX = 1\n",
                            spec=task.deliverables[0], task=task)
        review.append(task.model_copy(update={"status": S.REVIEW}))

    board, sch = build(env, [*review, *[mk(t) for t in "EFGH"]],
                       max_concurrency=2)
    await asyncio.wait_for(sch.run(), timeout=10)

    assert sch.deadlock_report is None
    assert all(board.get(t).status is S.SUCCEEDED for t in "ABCDEFGH")
    assert sch.peak_concurrency <= 2


# ── 并发峰值是实测值 ──

async def test_peak_concurrency_reaches_limit_across_eight_dispatches(env):
    for t in "ABCDEFGH":
        write_fixture(env["fx"], t)
    board, sch = build(env, [mk(t) for t in "ABCDEFGH"], max_concurrency=3)
    await sch.run()
    assert sch.peak_concurrency == 3
    assert sch.peak_concurrency <= 3


async def test_peak_concurrency_tracks_actual_workers_below_limit(env):
    """任务数少于上限时峰值为真实并发数（不是回读配置）。"""
    write_fixture(env["fx"], "A"); write_fixture(env["fx"], "B")
    board, sch = build(env, [mk("A"), mk("B")], max_concurrency=4)
    await sch.run()
    assert board.get("A").status is S.SUCCEEDED
    assert board.get("B").status is S.SUCCEEDED
    assert sch.peak_concurrency == 2


# ── 取消语义（软/硬）──

async def test_soft_cancel_lets_inflight_worker_finish(env):
    write_fixture(env["fx"], "A"); write_fixture(env["fx"], "B")
    board, sch = build_slow(env, [mk("A"), mk("B")], latency_ms=150,
                            max_concurrency=1)

    async def canceller():
        await asyncio.sleep(0.02); sch.cancel()

    await asyncio.wait_for(asyncio.gather(sch.run(), canceller()), timeout=3)
    assert board.get("A").status is S.SUCCEEDED     # 在跑的让它跑完
    assert board.get("B").status is not S.SUCCEEDED  # 不再派发新任务


async def test_hard_cancel_cancels_inflight_worker(env):
    write_fixture(env["fx"], "A"); write_fixture(env["fx"], "B")
    board, sch = build_slow(env, [mk("A"), mk("B")], latency_ms=150,
                            max_concurrency=1)

    async def canceller():
        await asyncio.sleep(0.02); sch.cancel(hard=True)

    await asyncio.wait_for(asyncio.gather(sch.run(), canceller()), timeout=3)
    assert board.get("A").status is S.CANCELLED
    assert board.get("B").status is not S.SUCCEEDED


# ── 夹具与事件的可观测性 ──

async def test_llm_gate_uses_review_fixture(env):
    """G2 的 approve 裁决来自 ``_default`` 夹具（score=0.95），不是格式错误放行。"""
    write_fixture(env["fx"], "A")
    board, sch = build(env, [mk("A")])
    await sch.run()
    llm = [r for r in board.get("A").reviews if r.layer == "llm"]
    assert len(llm) == 1
    assert llm[0].verdict == "approve" and llm[0].score == 0.95


async def test_on_event_and_ledger_transitions_are_wired(env):
    write_fixture(env["fx"], "A")
    seen: list[tuple[str, dict]] = []
    board = TaskBoard([mk("A")])
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"],
                    on_event=lambda name, data: seen.append((name, data)))
    await sch.run()
    names = [name for name, _ in seen]
    assert "task_dispatched" in names and "task_completed" in names
    transitions = [e for e in env["ledger"].events("task_transition")]
    assert any(e.data["task_id"] == "A" and e.data["to"] == "review"
               for e in transitions)


async def test_gate_evaluates_after_task_slot_release(env):
    """结构性守卫：门禁执行时任务槽位必须已归还（不靠超时兜底）。

    ``max_concurrency=1`` 下，``evaluate`` 期间 ``locked()`` 为真即说明门禁被
    放进了任务槽位内部。实测：``tests/test_scheduler_deadlock.py`` 的三个超时
    用例对这种内嵌实现**全部通过**（单任务下两级限额不构成循环等待），只有这里
    的直接观测能拦下它。
    """
    write_fixture(env["fx"], "A")
    board, sch = build(env, [mk("A")], max_concurrency=1)
    inner = sch._gatekeeper
    locked_during_gate: list[bool] = []

    class Probe:
        async def evaluate(self, task, *, round_no):
            locked_during_gate.append(sch._task_slot.locked())
            return await inner.evaluate(task, round_no=round_no)

    sch._gatekeeper = Probe()
    await sch.run()
    assert board.get("A").status is S.SUCCEEDED
    assert locked_during_gate == [False]
