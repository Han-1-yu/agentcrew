"""Task 33 · 端到端：整条流水线在离线夹具上跑通（spec §16.1）。

断言的是**结局**而不是内部实现：run 状态与统计、`MANIFEST.json` 与磁盘（含
sha256）的一致性、生成的 Python/JSON/YAML 真的可解析、ledger 里的事件序列完整、
G2 评审层确实覆盖了每个过了 G1 的任务、没有发生任何 fixture 回退，以及 T-005
的「驳回 → 返工 → 通过」闭环真的发生过。

全部用例读 ``offline_run``（conftest，模块级共享）里那一次 run。该 run 在
**删光全部 API Key 且不加载 ``.env``** 的前提下执行，因此本文件的每条断言都
顺带证明了「无密钥可跑」——单次 run 约 0.9 秒，逐用例重跑会把套件拖慢数秒。
"""

import ast
import hashlib
import json

import yaml

from agentcrew.runtime.checkpoint import RunStatus

#: 计划夹具里的任务数（``examples/fixtures/plan_todo.json``）。
PLANNED_TASKS = 8


def test_full_run_completes(offline_run):
    rec = offline_run.record
    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.failed == 0 and rec.stats.blocked == 0
    assert rec.stats.total_tasks == PLANNED_TASKS + 1  # 8 个计划任务 + T-FINAL
    assert rec.stats.succeeded == PLANNED_TASKS + 1


def test_artifacts_are_real_files(offline_run):
    """清单声明的每个产物都必须真的在盘上，且字节数与 sha256 都对得上。

    只比字节数抓不住**同尺寸覆写**：一个绕过 ``ArtifactStore`` 把内容换成等长
    文件的动作，尺寸断言照样通过。哈希是清单对内容唯一的承诺，必须逐条验。
    """
    art = offline_run.artifacts
    manifest = json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"]
    for entry in manifest["entries"]:
        p = art / entry["path"]
        assert p.is_file(), f"清单声明了 {entry['path']} 但文件不存在"
        data = p.read_bytes()
        assert len(data) == entry["size_bytes"], f"{entry['path']} 字节数与清单不符"
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], (
            f"{entry['path']} 的内容与清单记录的 sha256 不一致（可能被越过 "
            "ArtifactStore 覆写）"
        )


def test_all_generated_python_parses(offline_run):
    parsed = 0
    for p in offline_run.artifacts.rglob("*.py"):
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        parsed += 1
    assert parsed > 0, "一个 .py 产物都没有，上面的循环是空转，不能算通过"


def test_json_and_yaml_artifacts_parse(offline_run):
    """盘上每个 ``.json`` / ``.yaml`` 都必须能解析。

    JSON 侧不能用「计数 > 0」当非空守卫：``rglob("*.json")`` 永远匹配得到 store
    自己写的 ``MANIFEST.json``，那样的守卫恒真。本演示的非清单 JSON 交付物恰好
    为零，所以这里断言的是**集合恒等**——盘上的非清单 ``.json`` 必须与清单里声明
    的 ``.json`` 交付物一一对应（多出一个未经契约的文件、或漏掉一个声明的交付物，
    都会在这里现形），YAML 侧才是「真实交付物非空」的计数守卫。
    """
    art = offline_run.artifacts
    manifest = json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))

    parsed_json: set[str] = set()
    for p in art.rglob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))
        parsed_json.add(p.relative_to(art).as_posix())
    assert "MANIFEST.json" in parsed_json, "store 写的清单本身也必须可解析"

    on_disk_json = parsed_json - {"MANIFEST.json"}
    declared_json = {
        entry["path"] for entry in manifest["entries"]
        if entry["path"].endswith(".json")
    }
    assert on_disk_json == declared_json, (
        f"非清单 .json 与契约不符：多出 {sorted(on_disk_json - declared_json)}、"
        f"缺少 {sorted(declared_json - on_disk_json)}"
    )

    yaml_files = 0
    for p in art.rglob("*.yaml"):
        yaml.safe_load(p.read_text(encoding="utf-8"))
        yaml_files += 1
    assert yaml_files > 0, "一个 .yaml 产物都没有，上面的循环是空转"


def test_ledger_records_llm_calls_and_transitions(offline_run):
    """ledger 必须完整记下四类事件，且 ``run_finished`` 恰好一条。"""
    lg = offline_run.ledger
    assert lg.count("llm_call") > 0
    assert lg.count("task_transition") > 0
    assert lg.count("artifact_written") > 0
    assert lg.count("run_finished") == 1


def test_g2_llm_review_ran_for_every_task(offline_run):
    """G2（LLM 评审）层必须真的跑过，且覆盖每一个过了 G1 的任务。

    G1 一层就能让全部 9 个任务通过、整条 run 变绿，``llm_call > 0`` 也会被生成
    调用满足 —— 一个「悄悄不再调用评审器」的实现因此能骗过其它全部断言。这条
    直接把两层对齐：凡是 G1 放行的任务，都必须有一条 ``layer="llm"`` 的评审。
    """
    events = list(offline_run.ledger.events("gate_result"))
    llm_reviewed = {
        e.data["task_id"] for e in events if e.data.get("layer") == "llm"
    }
    assert llm_reviewed, "没有任何 layer='llm' 的 gate_result —— G2 层被整个跳过了"

    static_approved = {
        e.data["task_id"]
        for e in events
        if e.data.get("layer") == "static" and e.data.get("verdict") == "approve"
    }
    assert len(static_approved) == PLANNED_TASKS + 1  # 9 个任务都过了 G1
    assert llm_reviewed == static_approved, (
        f"这些任务过了 G1 却没有 G2 评审：{sorted(static_approved - llm_reviewed)}"
    )

    # 审计事件还可能是「空评审」：``Reviewer`` 在角色没有配评审人时会直接放行并
    # 照发一条 ``layer="llm"`` 的 approve（reviewer.py 的早退分支），事件层面看起来
    # 完全正常。真正证明评审跑过的是账本里的评审开销 —— 每个过了 G1 的任务都必须
    # 至少烧掉一次 ``purpose="review"`` 的 LLM 调用。
    review_calls = [
        e for e in offline_run.ledger.events("llm_call")
        if e.data.get("purpose") == "review"
    ]
    assert len(review_calls) >= len(static_approved), (
        f"只有 {len(review_calls)} 次评审调用，却有 {len(static_approved)} 个任务"
        "过了 G1 —— G2 是空评审"
    )


def test_g2_never_fell_back_to_format_error(offline_run):
    """G2 的裁决必须是**真裁决**，不能是「评审器返回格式错误，已放行」。

    上面的覆盖断言只看事件层：夹具漂移出 ``ReviewVerdict``（或缺失）时，
    ``MockProvider`` 的回放进不了 schema，``Reviewer`` 走格式错误通道放行，
    ``layer="llm"`` 的 ``gate_result`` 与 ``purpose="review"`` 的 ``llm_call``
    一样照常产生 —— 一次「全绿却没有一次真评审」的 run 因此能骗过全部断言。
    这里直接读裁决里的 issues：真裁决不会带这条放行说明。
    """
    from agentcrew.gate.reviewer import _FORMAT_ERROR_ISSUE

    llm_verdicts = [
        e for e in offline_run.ledger.events("gate_result")
        if e.data.get("layer") == "llm"
    ]
    assert llm_verdicts
    fell_back = [
        e.data.get("task_id")
        for e in llm_verdicts
        if _FORMAT_ERROR_ISSUE in (e.data.get("issues") or [])
    ]
    assert fell_back == [], (
        f"这些任务的 G2 走了「格式错误放行」通道：{sorted(fell_back)} —— "
        "评审夹具已漂移出 ReviewVerdict")


def test_no_mock_fixture_fallback(offline_run):
    """一次干净的演示不允许出现任何 fixture 回退（spec §16.1：绝不静默）。

    ``mock_fallback`` 事件意味着某次回放落到了「改用 attempt-1」或占位回复 ——
    产物就不再是夹具里那份，run 却照样能完成。回退必须为 0 才是真演示。
    """
    assert offline_run.ledger.count("mock_fallback") == 0


def test_rework_demo_happened(offline_run):
    """T-005 的 attempt-1 不合格，必须出现一次返工并恢复。"""
    lg = offline_run.ledger
    assert lg.count("gate_result") > PLANNED_TASKS + 1  # 至少有一次额外评审

    t005 = [e for e in lg.events("gate_result") if e.data.get("task_id") == "T-005"]
    rejected = [e for e in t005 if e.data.get("verdict") == "reject"]
    assert rejected, "T-005 从未被打回，返工闭环没有发生"
    assert any(
        e.data.get("layer") == "static" and e.data.get("round") == 1
        for e in rejected
    ), "T-005 的第一次驳回必须来自 G1 静态门禁（第 1 轮）"

    recovered = [
        e
        for e in t005
        if e.data.get("verdict") == "approve" and e.data.get("round", 0) >= 2
    ]
    assert recovered, "T-005 被打回后没有在返工轮通过"


def test_no_api_keys_required(offline_run):
    """没有任何 API Key 也必须能跑完整条流水线（spec §16.1）。"""
    missing = {
        name: value
        for name, value in offline_run.keys_at_run_time.items()
        if value is not None
    }
    assert not missing, f"run 启动时这些 Key 还在环境里：{sorted(missing)}"
    assert offline_run.settings.mock is True
    assert offline_run.settings.api_key_for("openai") is None
    assert offline_run.record.status is RunStatus.COMPLETED


def test_delivery_md_mentions_completion(offline_run):
    """交付文档必须真的声明「完成清单」。

    只找 ``"完成"`` 是不够的：``"未完成"`` 也包含它。取标题这一整段，
    既满足 brief 的「含完成」，又不给反向措辞留后门。
    """
    text = (offline_run.artifacts / "DELIVERY.md").read_text(encoding="utf-8")
    assert "## 完成清单" in text
